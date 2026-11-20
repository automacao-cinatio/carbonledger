"""Perfis, resolucao do acesso a cada requisicao e protecao contra CSRF.

A cada requisicao com sessao, o acesso (perfil e empresas) e lido de novo no banco pela
funcao identidade_acesso: membro desativado ou empresa retirada perdem o acesso na hora.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.core.erros import CORPO_NAO_ENCONTRADO, AcessoNegado
from app.core.security.sessao import ArmazemSessoes, Sessao, tokens_iguais
from app.core.tenancy import ATRIBUTO_REQUISICAO, ContextoTenant, Escopo

CABECALHO_CSRF = "X-CSRF-Token"
METODOS_SEGUROS = frozenset({"GET", "HEAD", "OPTIONS"})


class Perfil(StrEnum):
    ADMINISTRADOR = "administrador"
    GESTOR = "gestor"
    RESPONSAVEL = "responsavel"
    COLABORADOR = "colaborador"

    @property
    def ve_o_tenant_inteiro(self) -> bool:
        return self in (Perfil.ADMINISTRADOR, Perfil.GESTOR)


@dataclass(frozen=True)
class Acesso:
    usuario_id: UUID
    tenant_id: UUID
    perfil: Perfil
    empresas: frozenset[UUID]

    def contexto(self) -> ContextoTenant:
        if self.perfil.ve_o_tenant_inteiro:
            return ContextoTenant(
                tenant_id=self.tenant_id, escopo=Escopo.TENANT, usuario_id=self.usuario_id
            )
        return ContextoTenant(
            tenant_id=self.tenant_id,
            escopo=Escopo.EMPRESAS,
            empresas=self.empresas,
            usuario_id=self.usuario_id,
        )


def nomes_dos_cookies(seguro: bool) -> tuple[str, str]:
    """(sessao, login). Com HTTPS, o prefixo __Host- prende o cookie ao dominio exato."""
    prefixo = "__Host-" if seguro else ""
    return f"{prefixo}cl_sessao", f"{prefixo}cl_login"


async def resolver_acesso(
    fabrica: async_sessionmaker[AsyncSession], usuario_id: UUID, tenant_id: UUID
) -> Acesso | None:
    async with fabrica() as sessao:
        linha = (
            await sessao.execute(
                text("SELECT perfil, empresas FROM identidade_acesso(:usuario, :tenant)"),
                {"usuario": usuario_id, "tenant": tenant_id},
            )
        ).first()
    if linha is None:
        return None
    return Acesso(usuario_id, tenant_id, Perfil(linha.perfil), frozenset(linha.empresas))


class MiddlewareSessao(BaseHTTPMiddleware):
    """Le a sessao do cookie, confere o CSRF e deixa acesso e contexto em request.state."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        nome_cookie, _ = nomes_dos_cookies(getattr(request.app.state, "cookie_seguro", True))
        token = request.cookies.get(nome_cookie)
        if not token:
            # Sem cookie nao ha sessao a ler: nem toca no Redis (o /saude passa por aqui).
            return await call_next(request)
        armazem: ArmazemSessoes = request.app.state.sessoes
        sessao = await armazem.obter(token)
        if sessao is not None:
            # Com cookie de sessao, toda escrita exige o token CSRF da propria sessao.
            if request.method not in METODOS_SEGUROS and not tokens_iguais(
                request.headers.get(CABECALHO_CSRF), sessao.csrf
            ):
                return JSONResponse(status_code=404, content=CORPO_NAO_ENCONTRADO)
            request.state.sessao = sessao
            if sessao.tenant_id is not None:
                acesso = await resolver_acesso(
                    request.app.state.fabrica_sessoes, sessao.usuario_id, sessao.tenant_id
                )
                if acesso is None:
                    request.state.sessao = await armazem.trocar_tenant(sessao, None)
                else:
                    request.state.acesso = acesso
                    setattr(request.state, ATRIBUTO_REQUISICAO, acesso.contexto())
        return await call_next(request)


def obter_sessao_usuario(request: Request) -> Sessao:
    sessao = getattr(request.state, "sessao", None)
    if not isinstance(sessao, Sessao):
        raise AcessoNegado
    return sessao


def exigir_perfil(*perfis: Perfil) -> Callable[[Request], Acesso]:
    permitidos = frozenset(perfis)

    def dependencia(request: Request) -> Acesso:
        acesso = getattr(request.state, "acesso", None)
        if not isinstance(acesso, Acesso) or acesso.perfil not in permitidos:
            raise AcessoNegado
        return acesso

    return dependencia
