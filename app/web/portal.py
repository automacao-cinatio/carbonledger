"""Contexto comum das telas: quem esta logado, cliente, empresas, inventario e token CSRF."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security.acesso import Acesso, Perfil
from app.core.security.sessao import Sessao
from app.core.tenancy import sessao_de_tenant
from app.modules.cobranca.bloqueio import em_atraso
from app.web.renderizacao import templates

ROTULOS_PERFIL = {
    Perfil.ADMINISTRADOR: "Administrador",
    Perfil.GESTOR: "Gestor",
    Perfil.RESPONSAVEL: "Responsável",
    Perfil.COLABORADOR: "Colaborador",
}


@dataclass(frozen=True)
class EmpresaVisivel:
    id: str
    nome: str


@dataclass(frozen=True)
class AcessoCliente:
    tenant_id: str
    nome: str
    perfil_rotulo: str


@dataclass(frozen=True)
class Inventario:
    ano: int
    estado: str
    coleta: tuple[int, int]
    evidencia: tuple[int, int]


@dataclass(frozen=True)
class Portal:
    usuario_nome: str | None = None
    tenant_nome: str | None = None
    perfil: Perfil | None = None
    acessos: list[AcessoCliente] = field(default_factory=list)
    empresas: list[EmpresaVisivel] = field(default_factory=list)
    empresa_atual: str | None = None
    inventario: Inventario | None = None
    csrf: str | None = None
    # Pagamento em atraso: aviso em todas as telas (o bloqueio fica nas acoes novas).
    em_atraso: bool = False


PORTAL_ANONIMO = Portal()


async def _inventario_da_empresa(db: AsyncSession, empresa_id: UUID) -> Inventario | None:
    """Inventario mais recente da empresa em foco, com o progresso de coleta e de evidencia."""
    # Import tardio: o modulo de coleta usa os formatos da web, e o portal usa a coleta.
    from app.modules.coleta import servico

    inventario = await servico.inventario_em_foco(db, empresa_id)
    if inventario is None:
        return None
    total = servico.progresso_total(await servico.situacao_das_fontes(db, inventario.inventario_id))
    return Inventario(
        ano=inventario.ano,
        estado=inventario.estado_rotulo,
        coleta=(total.aprovados, total.esperados),
        evidencia=(total.com_evidencia, total.esperados),
    )


async def montar_portal(request: Request) -> Portal:
    sessao = getattr(request.state, "sessao", None)
    if not isinstance(sessao, Sessao):
        return PORTAL_ANONIMO
    fabrica: async_sessionmaker[AsyncSession] = request.app.state.fabrica_sessoes
    async with fabrica() as db:
        usuario = (
            await db.execute(
                text("SELECT nome FROM identidade_usuario(:usuario)"),
                {"usuario": sessao.usuario_id},
            )
        ).first()
        linhas = (
            await db.execute(
                text("SELECT tenant_id, tenant_nome, perfil FROM identidade_acessos(:usuario)"),
                {"usuario": sessao.usuario_id},
            )
        ).all()
    acessos = [
        AcessoCliente(str(a.tenant_id), a.tenant_nome, ROTULOS_PERFIL[Perfil(a.perfil)])
        for a in linhas
    ]
    acesso = getattr(request.state, "acesso", None)
    if not isinstance(acesso, Acesso):
        return Portal(
            usuario_nome=None if usuario is None else usuario.nome,
            acessos=acessos,
            csrf=sessao.csrf,
        )

    async with sessao_de_tenant(fabrica, acesso.contexto()) as db:
        empresas = [
            EmpresaVisivel(str(e.empresa_id), e.nome)
            for e in await db.execute(
                text("SELECT empresa_id, nome FROM empresa WHERE ativa ORDER BY nome")
            )
        ]
        ids = {e.id for e in empresas}
        escolhida = str(sessao.empresa_id) if sessao.empresa_id else None
        atual = escolhida if escolhida in ids else (empresas[0].id if empresas else None)
        inventario = None if atual is None else await _inventario_da_empresa(db, UUID(atual))
        atraso = await em_atraso(db)
    tenant_nome = next((a.nome for a in acessos if a.tenant_id == str(acesso.tenant_id)), None)
    return Portal(
        usuario_nome=None if usuario is None else usuario.nome,
        tenant_nome=tenant_nome,
        perfil=acesso.perfil,
        acessos=acessos,
        empresas=empresas,
        empresa_atual=atual,
        inventario=inventario,
        csrf=sessao.csrf,
        em_atraso=atraso,
    )


def renderizar(
    request: Request,
    template: str,
    portal: Portal,
    *,
    pagina_atual: str = "",
    status_code: int = 200,
    **contexto: Any,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        template,
        {"portal": portal, "pagina_atual": pagina_atual, **contexto},
        status_code=status_code,
    )


def empresa_visivel(portal: Portal, empresa_id: UUID) -> EmpresaVisivel | None:
    return next((e for e in portal.empresas if e.id == str(empresa_id)), None)
