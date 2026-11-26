"""Cobranca (P22): tela do administrador, webhook do gateway e o simulador de pagamento.

- /cobranca: so o administrador; situacao da assinatura, tabela de precos, empresas ativas,
  estimativa do proximo mes e faturas com o link de pagamento.
- /cobranca/pagador: o administrador grava razao social, CNPJ e e-mail de cobranca.
- /cobranca/webhook/{gateway}: sem sessao; o gateway confere o token. Localiza o tenant pela
  funcao cobranca_localizar e processa com o contexto dele. Token invalido: 404, sem detalhe.
  Token valido com evento que nao e de uma fatura da plataforma (outra cobranca da conta, ou
  evento sem cobranca): 200, ignorado. O Asaas reenvia o que nao recebe 200 e pausa a fila
  depois de 15 falhas seguidas. Um pagamento que chegue antes da fatura gravada (ou depois de
  uma rodada desfeita) nao se perde: a rodada diaria concilia as faturas em aberto com o
  gateway antes dos vencimentos.
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
from app.core.erros import AcessoNegado, ErrosDeValidacao, ErroValidacao
from app.core.security.acesso import Acesso, Perfil, exigir_perfil
from app.core.tenancy import ContextoTenant, Escopo, conexao_de_tenant
from app.modules.cobranca import precos, servico
from app.modules.cobranca.gateway import Pagador, do_simulador
from app.modules.organizacao.acessos import normalizar_email
from app.modules.organizacao.estrutura import validar_cnpj
from app.modules.organizacao.validacao import formatar_cnpj
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
        "pagador": {"nome": "", "cnpj": "", "email": ""},
        "falta_pagador": False,
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
        if assinatura.pagador_cnpj is not None:
            contexto["pagador"] = {
                "nome": assinatura.pagador_nome,
                "cnpj": formatar_cnpj(assinatura.pagador_cnpj),
                "email": assinatura.pagador_email,
            }
    gateway = servico.gateway_da_configuracao(configuracao)
    contexto["falta_pagador"] = (
        gateway is not None and gateway.exige_pagador and not contexto["pagador"]["cnpj"]
    )
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


def _pagador_do_formulario(campos: dict[str, str]) -> Pagador:
    erros: list[ErroValidacao] = []
    nome = " ".join(campos.get("nome", "").split())
    if not nome:
        erros.append(ErroValidacao("nome", "Informe a razão social de quem paga."))
    elif len(nome) > 200:
        erros.append(ErroValidacao("nome", "A razão social passa de 200 caracteres. Abrevie."))
    cnpj, erro_cnpj = validar_cnpj(campos.get("cnpj", ""), obrigatorio=True)
    if erro_cnpj:
        erros.append(ErroValidacao("cnpj", erro_cnpj))
    email = ""
    try:
        email = normalizar_email(campos.get("email", ""))
    except ErroValidacao as erro:
        erros.append(erro)
    if erros:
        raise ErrosDeValidacao(erros)
    assert cnpj is not None  # noqa: S101  # nosec B101  # validar_cnpj obrigatorio devolve CNPJ
    return Pagador(nome, cnpj, email)


@router.post("/pagador")
async def salvar_pagador(request: Request, sessao: SessaoTenant, acesso: Administrador) -> Response:
    configuracao = obter_configuracao()
    parametros = servico.parametros_da_configuracao(configuracao)
    if parametros is None:
        raise AcessoNegado
    formulario = await request.form()
    pagador = _pagador_do_formulario({k: str(v) for k, v in formulario.items()})
    conexao = await conexao_asyncpg(sessao)
    await servico.garantir_assinatura(
        conexao, acesso.tenant_id, parametros.dias_teste, configuracao.cobranca_gateway
    )
    await servico.salvar_pagador(conexao, acesso.tenant_id, acesso.usuario_id, pagador)
    return resposta_com_aviso(
        "Dados de cobrança salvos. A próxima fatura sai com eles.", **{"HX-Refresh": "true"}
    )


# O evento de pagamento tem poucos KB. O corpo e lido antes de conferir o token (que o gateway
# confere junto com o evento): sem limite, qualquer um mandaria um corpo enorme para a memoria.
TAMANHO_MAXIMO_WEBHOOK = 64 * 1024


async def _corpo_limitado(request: Request) -> bytes | None:
    """Corpo ate TAMANHO_MAXIMO_WEBHOOK; None se passar (pelo cabecalho ou pelo que chegou)."""
    declarado = request.headers.get("content-length", "")
    if declarado.isdigit() and int(declarado) > TAMANHO_MAXIMO_WEBHOOK:
        return None
    partes = bytearray()
    async for pedaco in request.stream():
        partes.extend(pedaco)
        if len(partes) > TAMANHO_MAXIMO_WEBHOOK:
            return None
    return bytes(partes)


@router.post("/webhook/{gateway}")
async def webhook(gateway: str, request: Request) -> Response:
    configuracao = obter_configuracao()
    implementacao = servico.gateway_da_configuracao(configuracao)
    nao_encontrado = JSONResponse(status_code=404, content={"detail": "Não encontrado."})
    if implementacao is None or implementacao.nome != gateway:
        return nao_encontrado
    corpo = await _corpo_limitado(request)
    if corpo is None:
        return JSONResponse(status_code=413, content={"detail": "Corpo grande demais."})
    evento = implementacao.ler_evento({k.lower(): v for k, v in request.headers.items()}, corpo)
    if evento is None:
        return nao_encontrado
    if not evento.cobranca_id:
        return JSONResponse({"recebido": False})
    conexao = await asyncpg.connect(dsn_asyncpg(configuracao.database_url))
    try:
        achado = await servico.localizar(conexao, gateway, evento.cobranca_id)
    finally:
        await conexao.close()
    if achado is None:
        return JSONResponse({"recebido": False})
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
