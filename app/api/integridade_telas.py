"""Tela de integridade: ultimas verificacoes, falhas e o pedido de verificacao na hora.

Administrador e gestor. A verificacao pedida na tela roda na propria requisicao, pela mesma
funcao da tarefa diaria (integridade.verificar_tenant).
"""

import json
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from app.api.dependencias import SessaoTenant, conexao_asyncpg
from app.api.paginas import resposta_com_aviso
from app.core.security.acesso import Acesso, Perfil, exigir_perfil
from app.modules.integridade import servico
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
            "quando": linha.concluida_em.astimezone(FUSO).strftime("%d/%m/%Y %H:%M"),
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
    )


@router.post("/verificar")
async def verificar(request: Request, sessao: SessaoTenant, acesso: Gestao) -> Response:
    relatorio = await servico.verificar(
        await conexao_asyncpg(sessao),
        acesso.tenant_id,
        request.app.state.armazem,
        origem="manual",
        pedida_por=acesso.usuario_id,
    )
    if relatorio.integra:
        aviso = "Verificação concluída: tudo confere."
    else:
        aviso = f"Verificação concluída com {len(relatorio.falhas)} falha(s). Veja abaixo."
    return resposta_com_aviso(aviso, **{"HX-Refresh": "true"})
