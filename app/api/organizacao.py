"""Convites e membros do tenant. So o administrador usa estas rotas."""

from datetime import datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel
from sqlalchemy import text

from app.api.dependencias import SessaoTenant
from app.core import email as servico_email
from app.core.config import Configuracao, obter_configuracao
from app.core.security.acesso import Acesso, Perfil, exigir_perfil
from app.core.security.oidc import Provedor
from app.modules.organizacao import acessos

router = APIRouter(prefix="/api", tags=["organizacao"])

Administrador = Annotated[Acesso, Depends(exigir_perfil(Perfil.ADMINISTRADOR))]
Config = Annotated[Configuracao, Depends(obter_configuracao)]


class NovoConvite(BaseModel):
    email: str
    perfil: Perfil
    empresas: list[UUID] = []


class ConviteCriado(BaseModel):
    convite_id: UUID
    email: str
    perfil: Perfil
    expira_em: datetime


class AlteracaoDeMembro(BaseModel):
    perfil: Perfil
    empresas: list[UUID] = []


def links_do_convite(
    configuracao: Configuracao, provedores: dict[str, Provedor], token: str
) -> dict[str, str]:
    base = configuracao.app_url_base.rstrip("/")
    return {nome: f"{base}/convite/{token}/{nome}" for nome in provedores}


@router.post("/convites", status_code=status.HTTP_201_CREATED)
async def convidar(
    request: Request,
    dados: NovoConvite,
    sessao: SessaoTenant,
    acesso: Administrador,
    configuracao: Config,
) -> ConviteCriado:
    validade = timedelta(days=configuracao.convite_validade_dias)
    token = await acessos.criar_convite(
        sessao,
        tenant_id=acesso.tenant_id,
        criado_por=acesso.usuario_id,
        email=dados.email,
        perfil=dados.perfil,
        empresas=dados.empresas,
        validade=validade,
    )
    nome_tenant = await sessao.scalar(text("SELECT nome FROM tenant"))
    assunto, corpo = acessos.texto_do_convite(
        str(nome_tenant),
        links_do_convite(configuracao, request.app.state.provedores, token.codificar()),
        validade,
    )
    email_limpo = acessos.normalizar_email(dados.email)
    # Se o e-mail falhar, a excecao desfaz o convite (a sessao faz rollback).
    await servico_email.enviar_email(configuracao, email_limpo, assunto, corpo)
    criado = await sessao.execute(
        text("SELECT expira_em FROM convite WHERE convite_id = :convite"),
        {"convite": token.convite_id},
    )
    return ConviteCriado(
        convite_id=token.convite_id,
        email=email_limpo,
        perfil=dados.perfil,
        expira_em=criado.scalar_one(),
    )


@router.get("/convites")
async def listar_convites(sessao: SessaoTenant, _acesso: Administrador) -> list[dict[str, Any]]:
    return [
        {
            "convite_id": str(c.convite_id),
            "email": c.email,
            "perfil": c.perfil.value,
            "empresas": [str(e) for e in c.empresas],
            "criado_em": c.criado_em.isoformat(),
            "expira_em": c.expira_em.isoformat(),
            "situacao": c.situacao,
        }
        for c in await acessos.listar_convites(sessao)
    ]


@router.post("/convites/{convite_id}/revogar", status_code=status.HTTP_204_NO_CONTENT)
async def revogar_convite(convite_id: UUID, sessao: SessaoTenant, _acesso: Administrador) -> None:
    await acessos.revogar_convite(sessao, convite_id)


@router.get("/membros")
async def listar_membros(sessao: SessaoTenant, _acesso: Administrador) -> list[dict[str, Any]]:
    return [
        {
            "usuario_id": str(m.usuario_id),
            "nome": m.nome,
            "email": m.email,
            "perfil": m.perfil.value,
            "ativo": m.ativo,
            "empresas": [str(e) for e in m.empresas],
        }
        for m in await acessos.listar_membros(sessao)
    ]


@router.patch("/membros/{usuario_id}", status_code=status.HTTP_204_NO_CONTENT)
async def alterar_membro(
    usuario_id: UUID, dados: AlteracaoDeMembro, sessao: SessaoTenant, acesso: Administrador
) -> None:
    await acessos.alterar_membro(
        sessao, acesso, usuario_id, perfil=dados.perfil, empresas=dados.empresas
    )


@router.post("/membros/{usuario_id}/desativar", status_code=status.HTTP_204_NO_CONTENT)
async def desativar_membro(usuario_id: UUID, sessao: SessaoTenant, acesso: Administrador) -> None:
    await acessos.definir_ativo(sessao, acesso, usuario_id, ativo=False)


@router.post("/membros/{usuario_id}/reativar", status_code=status.HTTP_204_NO_CONTENT)
async def reativar_membro(usuario_id: UUID, sessao: SessaoTenant, acesso: Administrador) -> None:
    await acessos.definir_ativo(sessao, acesso, usuario_id, ativo=True)
