"""Meus dados (LGPD): o que a plataforma guarda sobre a pessoa, download e pedido de exclusao.

Qualquer pessoa com sessao, com ou sem cliente aberto. So os dados da propria pessoa, pelas
funcoes do app_identidade. O pedido de exclusao e executado pela plataforma (linha de comando),
porque a pessoa existe em todos os clientes e a anonimizacao e irreversivel.
"""

from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.paginas import resposta_com_aviso
from app.core.erros import ErroValidacao
from app.core.security.acesso import obter_sessao_usuario
from app.core.security.sessao import Sessao
from app.modules.lgpd import servico as lgpd
from app.web.portal import montar_portal, renderizar

router = APIRouter(prefix="/meus-dados", tags=["lgpd"], include_in_schema=False)

FUSO = ZoneInfo("America/Sao_Paulo")
SessaoAtual = Annotated[Sessao, Depends(obter_sessao_usuario)]

COLUNAS_ACESSOS = [
    {"chave": "tenant_nome", "titulo": "Cliente"},
    {"chave": "perfil", "titulo": "Perfil"},
]
COLUNAS_PEDIDOS = [
    {"chave": "solicitado", "titulo": "Pedido em"},
    {"chave": "situacao", "titulo": "Situação", "etiqueta": True},
]


def _fabrica(request: Request) -> async_sessionmaker[AsyncSession]:
    fabrica: async_sessionmaker[AsyncSession] = request.app.state.fabrica_sessoes
    return fabrica


@router.get("", response_class=HTMLResponse)
async def tela(request: Request, sessao: SessaoAtual) -> HTMLResponse:
    async with _fabrica(request)() as db:
        dados = await lgpd.meus_dados(db, sessao.usuario_id)
        lista = await lgpd.pedidos(db, sessao.usuario_id)
    pedidos = [
        {
            "solicitado": p.solicitado_em.astimezone(FUSO).strftime("%d/%m/%Y %H:%M"),
            "situacao": ("Executado", "success")
            if p.executado_em
            else ("Aguardando a plataforma", "warning"),
        }
        for p in lista
    ]
    return renderizar(
        request,
        "paginas/lgpd/meus_dados.html",
        await montar_portal(request),
        dados=dados,
        colunas_acessos=COLUNAS_ACESSOS,
        colunas_pedidos=COLUNAS_PEDIDOS,
        pedidos=pedidos,
        pedido_aberto=any(p.executado_em is None for p in lista),
    )


@router.get("/arquivo.json")
async def baixar(request: Request, sessao: SessaoAtual) -> Response:
    async with _fabrica(request)() as db:
        dados = await lgpd.meus_dados(db, sessao.usuario_id)
    return Response(
        content=lgpd.como_json(dados),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="meus-dados-carbonledger.json"',
            "Cache-Control": "private, no-store",
        },
    )


@router.post("/exclusao")
async def pedir_exclusao(request: Request, sessao: SessaoAtual) -> Response:
    async with _fabrica(request)() as db:
        dados = await lgpd.meus_dados(db, sessao.usuario_id)
        confirmacao = str((await request.form()).get("confirmacao", "")).strip().lower()
        if confirmacao != (dados["pessoa"]["email"] or "").lower():
            raise ErroValidacao(
                "confirmacao", "Para pedir a exclusão, digite exatamente o seu e-mail."
            )
        await lgpd.pedir_exclusao(db, sessao.usuario_id)
        await db.commit()
    return resposta_com_aviso(
        "Pedido de exclusão registrado. A plataforma executa em até 15 dias e avisa por e-mail.",
        **{"HX-Refresh": "true"},
    )
