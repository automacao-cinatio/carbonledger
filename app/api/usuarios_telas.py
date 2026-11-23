"""Usuarios e acessos do cliente: membros, convites, perfil, empresas e situacao.

So o administrador. As regras (perfil e empresas, nao mudar o proprio acesso, convite de uso
unico) moram em app/modules/organizacao/acessos.py, as mesmas da API /api/convites e
/api/membros. O e-mail do convite sai pelo mesmo servico de e-mail.
"""

from datetime import timedelta
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from app.api.dependencias import SessaoTenant
from app.api.organizacao import links_do_convite
from app.api.paginas import resposta_com_aviso
from app.core import email as servico_email
from app.core.config import Configuracao, obter_configuracao
from app.core.erros import AcessoNegado, ErroValidacao
from app.core.security.acesso import Acesso, Perfil, exigir_perfil
from app.modules.organizacao import acessos
from app.web.portal import ROTULOS_PERFIL, montar_portal, renderizar

router = APIRouter(prefix="/usuarios", tags=["usuarios"], include_in_schema=False)

FUSO = ZoneInfo("America/Sao_Paulo")

Administrador = Annotated[Acesso, Depends(exigir_perfil(Perfil.ADMINISTRADOR))]
Config = Annotated[Configuracao, Depends(obter_configuracao)]

OPCOES_PERFIL = [(p.value, ROTULOS_PERFIL[p]) for p in Perfil]
AJUDA_PERFIL = (
    "Administrador: tudo, inclusive usuários e verificação. Gestor: todas as empresas, abre e"
    " fecha inventários. Responsável: as empresas marcadas, cuida das fontes e aprova dados."
    " Colaborador: as empresas marcadas, lança dados e evidências."
)
SITUACOES_CONVITE = {
    "pendente": ("Pendente", "warning"),
    "aceito": ("Aceito", "success"),
    "revogado": ("Revogado", "secondary"),
    "expirado": ("Expirado", "secondary"),
}
COLUNAS_MEMBROS = [
    {"chave": "nome", "titulo": "Pessoa", "link": "url"},
    {"chave": "email", "titulo": "E-mail"},
    {"chave": "perfil", "titulo": "Perfil"},
    {"chave": "empresas", "titulo": "Empresas"},
    {"chave": "situacao", "titulo": "Situação", "etiqueta": True},
]
COLUNAS_CONVITES = [
    {"chave": "email", "titulo": "E-mail"},
    {"chave": "perfil", "titulo": "Perfil"},
    {"chave": "expira_em", "titulo": "Vale até"},
    {"chave": "situacao", "titulo": "Situação", "etiqueta": True},
]


async def _empresas(sessao: SessaoTenant) -> list[tuple[str, str]]:
    linhas = await sessao.execute(
        text("SELECT empresa_id, nome FROM empresa WHERE ativa ORDER BY nome")
    )
    return [(str(linha.empresa_id), linha.nome) for linha in linhas]


def _nomes(ids: list[UUID], empresas: list[tuple[str, str]]) -> str:
    nomes = dict(empresas)
    if not ids:
        return "Todas"
    return ", ".join(nomes.get(str(e), "Empresa desativada") for e in ids)


async def _dados(request: Request) -> tuple[dict[str, str], list[UUID]]:
    formulario = await request.form()
    try:
        empresas = [UUID(str(v)) for v in formulario.getlist("empresas")]
    except ValueError as erro:
        raise ErroValidacao("empresas", "Uma das empresas é inválida. Atualize a página.") from erro
    campos = {k: str(v) for k, v in formulario.items() if k != "empresas"}
    return campos, empresas


def _perfil(texto: str) -> Perfil:
    try:
        return Perfil(texto)
    except ValueError as erro:
        raise ErroValidacao("perfil", "Escolha o perfil.") from erro


@router.get("", response_class=HTMLResponse)
async def indice(request: Request, sessao: SessaoTenant, acesso: Administrador) -> HTMLResponse:
    empresas = await _empresas(sessao)
    membros = [
        {
            "nome": m.nome,
            "url": f"/usuarios/{m.usuario_id}",
            "email": m.email,
            "perfil": ROTULOS_PERFIL[m.perfil],
            "empresas": _nomes(m.empresas, empresas),
            "situacao": ("Ativo", "success") if m.ativo else ("Desativado", "secondary"),
        }
        for m in await acessos.listar_membros(sessao)
    ]
    convites = [
        {
            "convite_id": c.convite_id,
            "email": c.email,
            "perfil": ROTULOS_PERFIL[c.perfil],
            "expira_em": c.expira_em.astimezone(FUSO).strftime("%d/%m/%Y %H:%M"),
            "situacao": SITUACOES_CONVITE[c.situacao],
            "pendente": c.situacao == "pendente",
        }
        for c in await acessos.listar_convites(sessao)
    ]
    return renderizar(
        request,
        "paginas/usuarios/indice.html",
        await montar_portal(request),
        colunas_membros=COLUNAS_MEMBROS,
        membros=membros,
        colunas_convites=COLUNAS_CONVITES,
        convites=convites,
        perfis=OPCOES_PERFIL,
        ajuda_perfil=AJUDA_PERFIL,
        empresas=empresas,
        pendentes=[c for c in convites if c["pendente"]],
    )


@router.post("/convites")
async def convidar(
    request: Request, sessao: SessaoTenant, acesso: Administrador, configuracao: Config
) -> Response:
    campos, empresas = await _dados(request)
    validade = timedelta(days=configuracao.convite_validade_dias)
    token = await acessos.criar_convite(
        sessao,
        tenant_id=acesso.tenant_id,
        criado_por=acesso.usuario_id,
        email=campos.get("email", ""),
        perfil=_perfil(campos.get("perfil", "")),
        empresas=empresas,
        validade=validade,
    )
    nome_tenant = await sessao.scalar(text("SELECT nome FROM tenant"))
    assunto, corpo = acessos.texto_do_convite(
        str(nome_tenant),
        links_do_convite(configuracao, request.app.state.provedores, token.codificar()),
        validade,
    )
    # Se o e-mail falhar, a excecao desfaz o convite (a sessao faz rollback).
    await servico_email.enviar_email(
        configuracao, acessos.normalizar_email(campos.get("email", "")), assunto, corpo
    )
    return resposta_com_aviso("Convite enviado por e-mail.", **{"HX-Refresh": "true"})


@router.post("/convites/{convite_id}/revogar")
async def revogar_convite(
    convite_id: UUID, sessao: SessaoTenant, acesso: Administrador
) -> Response:
    await acessos.revogar_convite(sessao, acesso, convite_id)
    return resposta_com_aviso("Convite revogado.", **{"HX-Refresh": "true"})


async def _membro(sessao: SessaoTenant, usuario_id: UUID) -> acessos.Membro:
    membro = next(
        (m for m in await acessos.listar_membros(sessao) if m.usuario_id == usuario_id), None
    )
    if membro is None:
        raise AcessoNegado
    return membro


@router.get("/{usuario_id}", response_class=HTMLResponse)
async def ver_membro(
    request: Request, usuario_id: UUID, sessao: SessaoTenant, acesso: Administrador
) -> HTMLResponse:
    membro = await _membro(sessao, usuario_id)
    return renderizar(
        request,
        "paginas/usuarios/membro.html",
        await montar_portal(request),
        membro=membro,
        perfil_rotulo=ROTULOS_PERFIL[membro.perfil],
        e_voce=membro.usuario_id == acesso.usuario_id,
        perfis=OPCOES_PERFIL,
        ajuda_perfil=AJUDA_PERFIL,
        empresas=await _empresas(sessao),
        selecionadas=[str(e) for e in membro.empresas],
    )


@router.post("/{usuario_id}")
async def salvar_membro(
    request: Request, usuario_id: UUID, sessao: SessaoTenant, acesso: Administrador
) -> Response:
    campos, empresas = await _dados(request)
    await acessos.alterar_membro(
        sessao, acesso, usuario_id, perfil=_perfil(campos.get("perfil", "")), empresas=empresas
    )
    return resposta_com_aviso(
        "Acesso alterado. Vale a partir da próxima tela da pessoa.", **{"HX-Redirect": "/usuarios"}
    )


@router.post("/{usuario_id}/desativar")
async def desativar(usuario_id: UUID, sessao: SessaoTenant, acesso: Administrador) -> Response:
    await acessos.definir_ativo(sessao, acesso, usuario_id, ativo=False)
    return resposta_com_aviso("Pessoa desativada. O acesso foi cortado.", **{"HX-Refresh": "true"})


@router.post("/{usuario_id}/reativar")
async def reativar(usuario_id: UUID, sessao: SessaoTenant, acesso: Administrador) -> Response:
    await acessos.definir_ativo(sessao, acesso, usuario_id, ativo=True)
    return resposta_com_aviso("Pessoa reativada.", **{"HX-Refresh": "true"})
