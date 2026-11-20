"""Entrada por OIDC, aceite de convite, sessao e saida.

Toda falha de login responde 404, sem dizer se o motivo foi convite, e-mail, provedor ou
assinatura. O detalhe fica no log (sem dado pessoal).
"""

import secrets
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Configuracao, obter_configuracao
from app.core.erros import AcessoNegado
from app.core.security.acesso import nomes_dos_cookies, obter_sessao_usuario, resolver_acesso
from app.core.security.convite import decodificar_token
from app.core.security.oidc import (
    ClienteOidc,
    ErroOidc,
    Identificacao,
    Provedor,
    novo_verificador_pkce,
)
from app.core.security.sessao import ArmazemSessoes, Sessao, tokens_iguais

router = APIRouter(tags=["autenticacao"])
log = structlog.get_logger()

# Ate o portal existir (P05A), o login termina na descricao da sessao.
DESTINO_APOS_LOGIN = "/sessao"

# Erros que o banco usa para recusar convite: invalido (CL001) e empresa de outro tenant.
SQLSTATES_DE_CONVITE_RECUSADO = frozenset({"CL001", "23503"})

Config = Annotated[Configuracao, Depends(obter_configuracao)]
SessaoAtual = Annotated[Sessao, Depends(obter_sessao_usuario)]


def _provedor(request: Request, nome: str) -> Provedor:
    provedor: Provedor | None = request.app.state.provedores.get(nome)
    if provedor is None:
        raise AcessoNegado
    return provedor


def _redirect_uri(configuracao: Configuracao, provedor: Provedor) -> str:
    return f"{configuracao.app_url_base.rstrip('/')}/entrar/{provedor.nome}/retorno"


def _definir_cookie(
    resposta: Response, nome: str, valor: str, *, seguro: bool, duracao_s: int
) -> None:
    resposta.set_cookie(
        nome, valor, max_age=duracao_s, path="/", secure=seguro, httponly=True, samesite="lax"
    )


async def _iniciar(
    request: Request, configuracao: Configuracao, provedor: Provedor, convite: str | None
) -> RedirectResponse:
    cliente: ClienteOidc = request.app.state.oidc
    armazem: ArmazemSessoes = request.app.state.sessoes
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verificador, desafio = novo_verificador_pkce()
    try:
        url = await cliente.url_autorizacao(
            provedor,
            redirect_uri=_redirect_uri(configuracao, provedor),
            state=state,
            nonce=nonce,
            desafio=desafio,
        )
    except ErroOidc as erro:
        log.warning("login_falhou", provedor=provedor.nome, etapa="descoberta", erro=str(erro))
        raise AcessoNegado from erro
    await armazem.guardar_login(
        state,
        {"provedor": provedor.nome, "nonce": nonce, "verificador": verificador, "convite": convite},
    )
    resposta = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    _, cookie_login = nomes_dos_cookies(configuracao.cookie_seguro)
    # O state tambem fica num cookie: o retorno so vale no navegador que comecou o login.
    _definir_cookie(resposta, cookie_login, state, seguro=configuracao.cookie_seguro, duracao_s=600)
    return resposta


@router.get("/entrar/{provedor}")
async def entrar(request: Request, provedor: str, configuracao: Config) -> RedirectResponse:
    return await _iniciar(request, configuracao, _provedor(request, provedor), None)


@router.get("/convite/{token}/{provedor}")
async def aceitar_convite(
    request: Request, token: str, provedor: str, configuracao: Config
) -> RedirectResponse:
    if decodificar_token(token) is None:
        raise AcessoNegado
    return await _iniciar(request, configuracao, _provedor(request, provedor), token)


async def _usuario_do_login(
    fabrica: async_sessionmaker[AsyncSession], identificacao: Identificacao, convite: str | None
) -> tuple[UUID, UUID | None]:
    """(usuario, tenant a abrir). Sem convite, so entra quem ja tem identidade vinculada."""
    async with fabrica() as sessao:
        if convite is not None:
            token = decodificar_token(convite)
            if token is None or identificacao.email is None:
                raise AcessoNegado
            try:
                usuario_id = await sessao.scalar(
                    text(
                        "SELECT identidade_aceitar_convite(:tenant, :convite, :hash,"
                        " :provedor, :sujeito, :email, :nome)"
                    ),
                    {
                        "tenant": token.tenant_id,
                        "convite": token.convite_id,
                        "hash": token.segredo_hash,
                        "provedor": identificacao.provedor,
                        "sujeito": identificacao.sujeito,
                        "email": identificacao.email,
                        "nome": identificacao.nome,
                    },
                )
                await sessao.commit()
            except DBAPIError as erro:
                codigo = getattr(erro.orig, "sqlstate", None) or getattr(erro.orig, "pgcode", None)
                if codigo in SQLSTATES_DE_CONVITE_RECUSADO:
                    log.warning("login_falhou", provedor=identificacao.provedor, etapa="convite")
                    raise AcessoNegado from erro
                raise
            return UUID(str(usuario_id)), token.tenant_id

        usuario_id = await sessao.scalar(
            text("SELECT identidade_localizar(:provedor, :sujeito)"),
            {"provedor": identificacao.provedor, "sujeito": identificacao.sujeito},
        )
        if usuario_id is None:
            log.warning("login_falhou", provedor=identificacao.provedor, etapa="sem_vinculo")
            raise AcessoNegado
        tenants = (
            await sessao.execute(
                text("SELECT tenant_id FROM identidade_acessos(:usuario)"), {"usuario": usuario_id}
            )
        ).all()
    unico = tenants[0].tenant_id if len(tenants) == 1 else None
    return UUID(str(usuario_id)), unico


@router.get("/entrar/{provedor}/retorno")
async def retorno(
    request: Request,
    provedor: str,
    configuracao: Config,
    code: str | None = None,
    state: str | None = None,
) -> RedirectResponse:
    p = _provedor(request, provedor)
    armazem: ArmazemSessoes = request.app.state.sessoes
    cookie_sessao, cookie_login = nomes_dos_cookies(configuracao.cookie_seguro)

    if not code or not state or not tokens_iguais(request.cookies.get(cookie_login), state):
        log.warning("login_falhou", provedor=p.nome, etapa="state")
        raise AcessoNegado
    pendente = await armazem.consumir_login(state)
    if pendente is None or pendente.get("provedor") != p.nome:
        log.warning("login_falhou", provedor=p.nome, etapa="state")
        raise AcessoNegado

    cliente: ClienteOidc = request.app.state.oidc
    try:
        id_token = await cliente.trocar_codigo(
            p,
            codigo=code,
            redirect_uri=_redirect_uri(configuracao, p),
            verificador=str(pendente["verificador"]),
        )
        identificacao = await cliente.validar_id_token(p, id_token, str(pendente["nonce"]))
    except ErroOidc as erro:
        log.warning("login_falhou", provedor=p.nome, etapa="token", erro=str(erro))
        raise AcessoNegado from erro

    usuario_id, tenant_id = await _usuario_do_login(
        request.app.state.fabrica_sessoes, identificacao, pendente.get("convite")
    )
    token, _ = await armazem.criar(usuario_id, tenant_id)
    log.info("login", provedor=p.nome, tenant_id=None if tenant_id is None else str(tenant_id))

    resposta = RedirectResponse(DESTINO_APOS_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    _definir_cookie(
        resposta,
        cookie_sessao,
        token,
        seguro=configuracao.cookie_seguro,
        duracao_s=configuracao.sessao_maxima_h * 3600,
    )
    resposta.delete_cookie(cookie_login, path="/", secure=configuracao.cookie_seguro)
    return resposta


@router.get("/sessao")
async def descrever_sessao(request: Request, sessao: SessaoAtual) -> dict[str, Any]:
    fabrica: async_sessionmaker[AsyncSession] = request.app.state.fabrica_sessoes
    async with fabrica() as db:
        usuario = (
            await db.execute(
                text("SELECT email, nome FROM identidade_usuario(:usuario)"),
                {"usuario": sessao.usuario_id},
            )
        ).first()
        acessos = (
            await db.execute(
                text("SELECT tenant_id, tenant_nome, perfil FROM identidade_acessos(:usuario)"),
                {"usuario": sessao.usuario_id},
            )
        ).all()
    if usuario is None:
        raise AcessoNegado
    acesso = getattr(request.state, "acesso", None)
    return {
        "usuario": {"id": str(sessao.usuario_id), "nome": usuario.nome, "email": usuario.email},
        "tenant_atual": None if sessao.tenant_id is None else str(sessao.tenant_id),
        "perfil": None if acesso is None else acesso.perfil.value,
        "acessos": [
            {"tenant_id": str(a.tenant_id), "nome": a.tenant_nome, "perfil": a.perfil}
            for a in acessos
        ],
        "csrf": sessao.csrf,
    }


class EscolhaDeTenant(BaseModel):
    tenant_id: UUID


@router.post("/sessao/tenant", status_code=status.HTTP_204_NO_CONTENT)
async def escolher_tenant(request: Request, sessao: SessaoAtual, escolha: EscolhaDeTenant) -> None:
    acesso = await resolver_acesso(
        request.app.state.fabrica_sessoes, sessao.usuario_id, escolha.tenant_id
    )
    if acesso is None:
        raise AcessoNegado
    armazem: ArmazemSessoes = request.app.state.sessoes
    await armazem.trocar_tenant(sessao, escolha.tenant_id)


@router.post("/sair", status_code=status.HTTP_204_NO_CONTENT)
async def sair(request: Request, sessao: SessaoAtual, configuracao: Config) -> Response:
    armazem: ArmazemSessoes = request.app.state.sessoes
    await armazem.encerrar(sessao)
    resposta = Response(status_code=status.HTTP_204_NO_CONTENT)
    cookie_sessao, _ = nomes_dos_cookies(configuracao.cookie_seguro)
    resposta.delete_cookie(cookie_sessao, path="/", secure=configuracao.cookie_seguro)
    return resposta
