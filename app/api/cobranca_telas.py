"""Cobranca (P22): tela do administrador, webhook do gateway e o simulador de pagamento.

- /cobranca: so o administrador; situacao da assinatura, tabela de precos, empresas ativas,
  estimativa do proximo mes e faturas com o link de pagamento.
- /cobranca/webhook/{gateway}: sem sessao; o gateway confere o token. Localiza o tenant pela
  funcao cobranca_localizar e processa com o contexto dele. Token invalido ou cobranca
  desconhecida: 404, sem detalhe.
- /cobranca/simulador/...: so com o gateway simulado e fora de producao. Pagar ali processa o
  mesmo evento de um webhook de pagamento.
"""

from decimal import Decimal
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

from app.api.dependencias import SessaoTenant, conexao_asyncpg
from app.api.paginas import resposta_com_aviso
from app.core.config import obter_configuracao
from app.core.db import dsn_asyncpg
from app.core.erros import AcessoNegado
from app.core.security.acesso import Acesso, Perfil, exigir_perfil
from app.core.tenancy import ContextoTenant, Escopo, conexao_de_tenant
from app.modules.cobranca import precos, servico
from app.modules.cobranca.gateway import do_simulador
from app.web.portal import montar_portal, renderizar

router = APIRouter(prefix="/cobranca", tags=["cobranca"], include_in_schema=False)

FUSO = ZoneInfo("America/Sao_Paulo")
Administrador = Annotated[Acesso, Depends(exigir_perfil(Perfil.ADMINISTRADOR))]

SITUACOES_ASSINATURA = {
    "teste": ("Em período de teste", "info"),
    "ativa": ("Ativa", "success"),
    "inadimplente": ("Pagamento em atraso", "danger"),
    "cancelada": ("Cancelada", "secondary"),
}
SITUACOES_FATURA = {
    "aberta": ("Aberta", "info"),
    "paga": ("Paga", "success"),
    "vencida": ("Vencida", "danger"),
    "cancelada": ("Cancelada", "secondary"),
}
COLUNAS_FAIXAS = [
    {"chave": "faixa", "titulo": "Empresas ativas"},
    {"chave": "preco", "titulo": "Preço por empresa", "unidade": "R$/mês", "casas": 2},
]
COLUNAS_FATURAS = [
    {"chave": "competencia", "titulo": "Mês"},
    {"chave": "empresas", "titulo": "Empresas ativas", "unidade": "nº", "casas": 0},
    {"chave": "valor", "titulo": "Valor", "unidade": "R$", "casas": 2},
    {"chave": "vencimento", "titulo": "Vencimento"},
    {"chave": "situacao", "titulo": "Situação", "etiqueta": True},
    {"chave": "pagar", "titulo": "Pagamento", "link": "url"},
]


def _reais(centavos: int) -> Decimal:
    return Decimal(centavos) / 100


def _simulador_ligado() -> bool:
    configuracao = obter_configuracao()
    return configuracao.cobranca_gateway == "simulado" and not configuracao.em_producao


@router.get("", response_class=HTMLResponse)
async def indice(request: Request, sessao: SessaoTenant, acesso: Administrador) -> HTMLResponse:
    configuracao = obter_configuracao()
    parametros = servico.parametros_da_configuracao(configuracao)
    contexto: dict[str, Any] = {
        "ligada": parametros is not None,
        "assinatura": None,
        "situacao": None,
        "faixas": [],
        "faturas": [],
        "colunas_faixas": COLUNAS_FAIXAS,
        "colunas_faturas": COLUNAS_FATURAS,
        "empresas": 0,
        "estimativa": None,
    }
    if parametros is not None:
        anterior = 0
        for faixa in parametros.faixas:
            rotulo = (
                f"{anterior + 1} em diante" if faixa.ate is None
                else f"{anterior + 1} a {faixa.ate}"
            )  # fmt: skip
            contexto["faixas"].append({"faixa": rotulo, "preco": _reais(faixa.preco_centavos)})
            anterior = faixa.ate or anterior
        empresas = int(await sessao.scalar(text("SELECT count(*) FROM empresa WHERE ativa")) or 0)
        contexto["empresas"] = empresas
        contexto["estimativa"] = _reais(precos.calcular(parametros.faixas, empresas)[0])
    assinatura = (await sessao.execute(text("SELECT * FROM assinatura"))).first()
    if assinatura is not None:
        contexto["assinatura"] = assinatura
        contexto["situacao"] = SITUACOES_ASSINATURA[assinatura.situacao]
        contexto["teste_ate"] = assinatura.teste_ate.strftime("%d/%m/%Y")
    linhas = await sessao.execute(
        text(
            "SELECT competencia, empresas_ativas, valor_centavos, vencimento, situacao,"
            " url_pagamento FROM fatura ORDER BY competencia DESC"
        )
    )
    contexto["faturas"] = [
        {
            "competencia": f.competencia.strftime("%m/%Y"),
            "empresas": f.empresas_ativas,
            "valor": _reais(f.valor_centavos),
            "vencimento": f.vencimento.strftime("%d/%m/%Y"),
            "situacao": SITUACOES_FATURA[f.situacao],
            "pagar": "Pagar" if f.situacao in ("aberta", "vencida") and f.url_pagamento else "",
            "url": f.url_pagamento if f.situacao in ("aberta", "vencida") else None,
        }
        for f in linhas
    ]
    return renderizar(
        request, "paginas/cobranca/indice.html", await montar_portal(request), **contexto
    )


@router.post("/webhook/{gateway}")
async def webhook(gateway: str, request: Request) -> Response:
    configuracao = obter_configuracao()
    implementacao = servico.gateway_da_configuracao(configuracao)
    nao_encontrado = JSONResponse(status_code=404, content={"detail": "Não encontrado."})
    if implementacao is None or implementacao.nome != gateway:
        return nao_encontrado
    corpo = await request.body()
    evento = implementacao.ler_evento({k.lower(): v for k, v in request.headers.items()}, corpo)
    if evento is None:
        return nao_encontrado
    conexao = await asyncpg.connect(dsn_asyncpg(configuracao.database_url))
    try:
        achado = await servico.localizar(conexao, gateway, evento.cobranca_id)
    finally:
        await conexao.close()
    if achado is None:
        return nao_encontrado
    tenant_id, fatura_id = achado
    contexto = ContextoTenant(tenant_id=tenant_id, escopo=Escopo.TENANT)
    async with conexao_de_tenant(configuracao.database_url, contexto) as conexao_tenant:
        await servico.registrar_evento(conexao_tenant, tenant_id, fatura_id, gateway, evento)
    return JSONResponse({"recebido": True})


async def _fatura_do_simulador(sessao: SessaoTenant, cobranca_id: str) -> Any:
    if not _simulador_ligado():
        raise AcessoNegado
    fatura = (
        await sessao.execute(
            text(
                "SELECT fatura_id, competencia, valor_centavos, vencimento, situacao FROM fatura"
                " WHERE gateway = 'simulado' AND gateway_cobranca_id = :id"
            ),
            {"id": cobranca_id},
        )
    ).first()
    if fatura is None:
        raise AcessoNegado
    return fatura


@router.get("/simulador/{cobranca_id}", response_class=HTMLResponse)
async def simulador(
    request: Request, cobranca_id: str, sessao: SessaoTenant, _acesso: Administrador
) -> HTMLResponse:
    fatura = await _fatura_do_simulador(sessao, cobranca_id)
    return renderizar(
        request,
        "paginas/cobranca/simulador.html",
        await montar_portal(request),
        cobranca_id=cobranca_id,
        competencia=fatura.competencia.strftime("%m/%Y"),
        valor=_reais(fatura.valor_centavos),
        vencimento=fatura.vencimento.strftime("%d/%m/%Y"),
        paga=fatura.situacao == "paga",
    )


@router.post("/simulador/{cobranca_id}/pagar")
async def pagar_no_simulador(
    cobranca_id: str, sessao: SessaoTenant, acesso: Administrador
) -> Response:
    fatura = await _fatura_do_simulador(sessao, cobranca_id)
    await servico.registrar_evento(
        await conexao_asyncpg(sessao),
        acesso.tenant_id,
        fatura.fatura_id,
        "simulado",
        do_simulador(cobranca_id),
    )
    return resposta_com_aviso("Pagamento simulado confirmado.", **{"HX-Redirect": "/cobranca"})
