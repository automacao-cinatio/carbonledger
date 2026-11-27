"""Tela de integridade: ultimas verificacoes, falhas e o pedido de verificacao na hora.

Administrador e gestor. A verificacao pedida na tela vai para o worker (integridade_pedido,
tarefa integridade.processar_pedido), pela mesma funcao da verificacao diaria: num cliente
grande ela leva minutos e nao cabe na requisicao. A tela acompanha o pedido sozinha.
"""

import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from starlette.background import BackgroundTask

from app.api.dependencias import SessaoTenant, conexao_asyncpg
from app.api.paginas import resposta_com_aviso
from app.core.security.acesso import Acesso, Perfil, exigir_perfil
from app.modules.integridade import pedidos
from app.web.portal import montar_portal, renderizar

router = APIRouter(prefix="/integridade", tags=["integridade"], include_in_schema=False)

FUSO = ZoneInfo("America/Sao_Paulo")
Gestao = Annotated[Acesso, Depends(exigir_perfil(Perfil.ADMINISTRADOR, Perfil.GESTOR))]

TIPOS = {
    "trilha": "Trilha de auditoria",
    "calculo": "Cálculo",
    "fechamento": "Fechamento",
    "evidencias": "Evidência",
    "planilhas": "Planilha importada",
    "relatorios": "Relatório assinado",
}
COLUNAS_HISTORICO = [
    {"chave": "quando", "titulo": "Concluída em"},
    {"chave": "origem", "titulo": "Origem"},
    {"chave": "itens", "titulo": "Itens conferidos", "unidade": "itens", "casas": 0},
    {"chave": "situacao", "titulo": "Situação", "etiqueta": True},
]
COLUNAS_FALHAS = [
    {"chave": "tipo", "titulo": "O quê"},
    {"chave": "referencia", "titulo": "Onde"},
    {"chave": "motivo", "titulo": "Problema"},
]


def _hora(instante: datetime) -> str:
    return instante.astimezone(FUSO).strftime("%d/%m/%Y %H:%M")


def _pedido_na_tela(pedido: pedidos.Pedido) -> dict[str, Any]:
    return {
        "pedido_id": pedido.pedido_id,
        "situacao": pedido.situacao,
        "pedido_em": _hora(pedido.pedido_em),
        "mensagem": pedido.mensagem,
    }


def _json(valor: Any) -> Any:
    return json.loads(valor) if isinstance(valor, str) else valor


@router.get("", response_class=HTMLResponse)
async def indice(request: Request, sessao: SessaoTenant, _acesso: Gestao) -> HTMLResponse:
    linhas = list(
        await sessao.execute(
            text(
                "SELECT verificacao_id, origem, concluida_em, contagens, falhas, integra"
                " FROM integridade_verificacao ORDER BY concluida_em DESC LIMIT 20"
            )
        )
    )
    historico = [
        {
            "quando": _hora(linha.concluida_em),
            "origem": "Diária" if linha.origem == "agendada" else "Pedida na tela",
            "itens": sum(_json(linha.contagens).values()),
            "situacao": ("Íntegra", "success") if linha.integra else ("Com falhas", "danger"),
        }
        for linha in linhas
    ]
    ultima = linhas[0] if linhas else None
    falhas = (
        [
            {
                "tipo": TIPOS.get(f["tipo"], f["tipo"]),
                "referencia": f["referencia"],
                "motivo": f["motivo"],
            }
            for f in _json(ultima.falhas)
        ]
        if ultima is not None
        else []
    )
    contagens = _json(ultima.contagens) if ultima is not None else {}
    # So o pedido mais recente importa: na fila (a tela acompanha) ou falhou (o motivo).
    recente = await pedidos.ultimo(await conexao_asyncpg(sessao))
    na_fila = recente if recente is not None and recente.situacao == "na_fila" else None
    falhou = recente if recente is not None and recente.situacao == "falhou" else None
    return renderizar(
        request,
        "paginas/integridade/indice.html",
        await montar_portal(request),
        ultima=ultima,
        contagens=contagens,
        falhas=falhas,
        colunas_falhas=COLUNAS_FALHAS,
        historico=historico,
        colunas_historico=COLUNAS_HISTORICO,
        pedido_na_fila=None if na_fila is None else _pedido_na_tela(na_fila),
        pedido_falhou=None if falhou is None else _pedido_na_tela(falhou),
    )


@router.post("/verificar")
async def verificar(sessao: SessaoTenant, acesso: Gestao) -> Response:
    pedido_id = await pedidos.pedir(
        await conexao_asyncpg(sessao), tenant_id=acesso.tenant_id, usuario_id=acesso.usuario_id
    )
    resposta = resposta_com_aviso(
        "Verificação pedida. Num cliente grande leva alguns minutos; a tela avisa quando terminar.",
        **{"HX-Refresh": "true"},
    )
    # Depois da resposta: a sessao ja fez o commit, entao o worker encontra o pedido.
    resposta.background = BackgroundTask(
        enfileirar_verificacao, tenant_id=acesso.tenant_id, pedido_id=pedido_id
    )
    return resposta


def enfileirar_verificacao(*, tenant_id: UUID, pedido_id: UUID) -> None:
    """Manda o pedido para o worker. Os testes trocam esta funcao para rodar a tarefa na hora."""
    from app.workers.integridade import processar_pedido  # importa o Celery so quando precisa

    processar_pedido.delay(tenant_id=str(tenant_id), pedido_id=str(pedido_id))


@router.get("/pedidos/{pedido_id}/situacao")
async def situacao_do_pedido(
    request: Request, pedido_id: UUID, sessao: SessaoTenant, _acesso: Gestao
) -> Response:
    """Consultado pela tela a cada poucos segundos enquanto o pedido esta na fila."""
    pedido = await pedidos.obter(await conexao_asyncpg(sessao), pedido_id)
    if pedido.situacao == "na_fila":
        return renderizar(
            request,
            "paginas/integridade/pedido_na_fila.html",
            await montar_portal(request),
            pedido=_pedido_na_tela(pedido),
        )
    aviso = (
        "Verificação concluída. O resultado está na tela."
        if pedido.situacao == "concluido"
        else "A verificação não terminou. O motivo está na tela."
    )
    return resposta_com_aviso(aviso, **{"HX-Refresh": "true"})
