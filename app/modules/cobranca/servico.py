"""Cobranca por empresa ativa por mes (P22): assinatura, faturas, vencimento e pagamento.

Roda com asyncpg, na transacao com o contexto do tenant (worker diario e webhook):
- assinatura nasce em teste, com fim em criado_em do tenant + COBRANCA_DIAS_TESTE;
- depois do teste, uma fatura por mes (competencia), com as empresas ativas no dia da emissao
  e o valor pela tabela progressiva; o id da fatura deriva do tenant e da competencia, e o
  gateway e idempotente por ele: repetir a rodada nunca cobra duas vezes;
- fatura aberta passa do vencimento: vencida; vencida ha mais que a tolerancia: assinatura
  inadimplente (por enquanto so aviso; o que bloquear e decisao pendente);
- pagamento confirmado pelo gateway: fatura paga e, sem outra vencida, assinatura ativa. Cada
  evento do gateway e processado uma vez so. A rodada tambem pergunta ao gateway pelas faturas
  em aberto (conciliacao), para nao depender so do webhook;
- a assinatura fica travada (FOR UPDATE) durante a rodada: uma troca de pagador na tela espera
  e nao se perde;
- o que fala com o gateway roda em savepoint: se o gateway falha, vencimentos e inadimplencia
  ficam gravados e a falha vira aviso (a rodada do dia seguinte tenta de novo).
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid5

import structlog

from app.core import audit
from app.modules.cobranca import precos
from app.modules.cobranca.gateway import EVENTO_PAGO, Evento, Gateway, Pagador

log = structlog.get_logger(__name__)

# Namespace fixo: o id da fatura de um tenant num mes e sempre o mesmo.
NAMESPACE_FATURA = UUID("6f1c2b0e-8d4a-5e3b-9c7f-2a1d0b4e5c60")


@dataclass(frozen=True)
class Parametros:
    faixas: list[precos.Faixa]
    dias_teste: int
    dias_vencimento: int
    dias_tolerancia: int


@dataclass
class Rodada:
    situacao: str
    fatura_emitida: UUID | None = None
    vencidas: int = 0
    avisos: list[str] = field(default_factory=list)


AVISO_SEM_PAGADOR = "sem os dados do pagador: preencha na tela Cobrança para emitir a fatura"
AVISO_GATEWAY_EMISSAO = "o gateway falhou ao emitir a fatura: a rodada de amanhã tenta de novo"
AVISO_GATEWAY_CONCILIACAO = "o gateway falhou na conciliação: os pagamentos chegam pelo webhook"


def pagador_de(assinatura: Any) -> Pagador | None:
    if assinatura["pagador_cnpj"] is None:
        return None
    return Pagador(
        assinatura["pagador_nome"], assinatura["pagador_cnpj"], assinatura["pagador_email"]
    )


def competencia_de(dia: date) -> date:
    return dia.replace(day=1)


def id_da_fatura(tenant_id: UUID, competencia: date) -> UUID:
    return uuid5(NAMESPACE_FATURA, f"{tenant_id}:{competencia.isoformat()}")


async def garantir_assinatura(conexao: Any, tenant_id: UUID, dias_teste: int, gateway: str) -> Any:
    """A assinatura do tenant, criada se faltar, travada ate o fim da transacao."""
    linha = await conexao.fetchrow("SELECT * FROM assinatura FOR UPDATE")
    if linha is not None:
        return linha
    criado_em = await conexao.fetchval("SELECT criado_em FROM tenant")
    teste_ate = criado_em.date() + timedelta(days=dias_teste)
    await conexao.execute(
        "INSERT INTO assinatura (tenant_id, situacao, teste_ate, gateway)"
        " VALUES ($1, 'teste', $2, $3)",
        tenant_id,
        teste_ate,
        gateway,
    )
    return await conexao.fetchrow("SELECT * FROM assinatura FOR UPDATE")


async def _no_gateway(
    resultado: Rodada,
    aviso: str,
    tenant_id: UUID,
    conexao: Any,
    passo: Callable[[], Awaitable[Any]],
) -> bool:
    """Roda um passo que fala com o gateway num savepoint. Falhou: desfaz so o passo, registra
    o aviso e segue (o que a rodada ja gravou fica)."""
    try:
        async with conexao.transaction():
            await passo()
    except Exception:
        # Sem dado do pagador no log: o erro do gateway ja vem sem ele (ErroAsaas).
        log.exception("cobranca.gateway_falhou", tenant_id=str(tenant_id), aviso=aviso)
        resultado.avisos.append(aviso)
        return False
    return True


async def _marcar_paga(
    conexao: Any, tenant_id: UUID, fatura_id: UUID, dados: dict[str, Any]
) -> None:
    """Fatura paga (uma vez) e, sem outra vencida, assinatura inadimplente volta a ativa."""
    paga = await conexao.fetchval(
        "UPDATE fatura SET situacao = 'paga', paga_em = now()"
        " WHERE fatura_id = $1 AND situacao <> 'paga' RETURNING fatura_id",
        fatura_id,
    )
    if paga is not None:
        await audit.registrar_asyncpg(
            conexao,
            tenant_id=tenant_id,
            acao="cobranca.fatura_paga",
            entidade="fatura",
            entidade_id=fatura_id,
            dados=dados,
        )
    situacao = await conexao.fetchval("SELECT situacao FROM assinatura")
    pendente = await conexao.fetchval("SELECT count(*) FROM fatura WHERE situacao = 'vencida'")
    if situacao == "inadimplente" and not pendente:
        await _mudar_situacao(conexao, tenant_id, "inadimplente", "ativa")


async def _conciliar(conexao: Any, tenant_id: UUID, gateway: Gateway) -> None:
    """Pergunta ao gateway pelas faturas em aberto dele: pagamento cujo webhook se perdeu (ou
    chegou antes da fatura gravada) e marcado aqui, antes dos vencimentos."""
    abertas = await conexao.fetch(
        "SELECT fatura_id FROM fatura WHERE situacao IN ('aberta', 'vencida') AND gateway = $1",
        gateway.nome,
    )
    for linha in abertas:
        if await gateway.cobranca_paga(str(linha["fatura_id"])):
            await _marcar_paga(
                conexao,
                tenant_id,
                linha["fatura_id"],
                {"gateway": gateway.nome, "origem": "conciliacao"},
            )


async def _trocar_gateway(conexao: Any, tenant_id: UUID, de: str, para: str) -> Any:
    """O cliente do gateway antigo nao serve no novo: esquece o id e cria de novo na emissao."""
    await conexao.execute(
        "UPDATE assinatura SET gateway = $1, gateway_cliente_id = NULL,"
        " pagador_sincronizado = false, atualizada_em = now()",
        para,
    )
    await audit.registrar_asyncpg(
        conexao,
        tenant_id=tenant_id,
        acao="cobranca.gateway_trocado",
        entidade="assinatura",
        entidade_id=tenant_id,
        dados={"de": de, "para": para},
    )
    return await conexao.fetchrow("SELECT * FROM assinatura FOR UPDATE")


async def _mudar_situacao(conexao: Any, tenant_id: UUID, de: str, para: str) -> None:
    await conexao.execute("UPDATE assinatura SET situacao = $1, atualizada_em = now()", para)
    await audit.registrar_asyncpg(
        conexao,
        tenant_id=tenant_id,
        acao="cobranca.situacao_alterada",
        entidade="assinatura",
        entidade_id=tenant_id,
        dados={"de": de, "para": para},
    )


async def rodada(
    conexao: Any, tenant_id: UUID, hoje: date, parametros: Parametros, gateway: Gateway
) -> Rodada:
    """A rodada diaria de um tenant: vencimentos, inadimplencia e a fatura do mes."""
    assinatura = await garantir_assinatura(conexao, tenant_id, parametros.dias_teste, gateway.nome)
    if assinatura["gateway"] != gateway.nome:
        assinatura = await _trocar_gateway(conexao, tenant_id, assinatura["gateway"], gateway.nome)
    situacao = assinatura["situacao"]
    resultado = Rodada(situacao)
    if situacao == "cancelada":
        return resultado

    async def conciliar() -> None:
        await _conciliar(conexao, tenant_id, gateway)

    await _no_gateway(resultado, AVISO_GATEWAY_CONCILIACAO, tenant_id, conexao, conciliar)
    situacao = resultado.situacao = await conexao.fetchval("SELECT situacao FROM assinatura")
    resultado.vencidas = int(
        (
            await conexao.execute(
                "UPDATE fatura SET situacao = 'vencida' WHERE situacao = 'aberta'"
                " AND vencimento < $1",
                hoje,
            )
        ).split()[-1]
    )
    atrasada = await conexao.fetchval(
        "SELECT count(*) FROM fatura WHERE situacao = 'vencida' AND vencimento + $1::int < $2",
        parametros.dias_tolerancia,
        hoje,
    )
    if atrasada and situacao != "inadimplente":
        await _mudar_situacao(conexao, tenant_id, situacao, "inadimplente")
        situacao = resultado.situacao = "inadimplente"
    if hoje <= assinatura["teste_ate"]:
        return resultado
    competencia = competencia_de(hoje)
    fatura_id = id_da_fatura(tenant_id, competencia)
    if await conexao.fetchval("SELECT 1 FROM fatura WHERE fatura_id = $1", fatura_id):
        return resultado
    empresas = int(await conexao.fetchval("SELECT count(*) FROM empresa WHERE ativa"))
    if empresas == 0:
        resultado.avisos.append("sem empresa ativa: nada a cobrar neste mes")
        return resultado
    pagador = pagador_de(assinatura)
    if pagador is None and gateway.exige_pagador:
        resultado.avisos.append(AVISO_SEM_PAGADOR)
        return resultado
    valor, linhas = precos.calcular(parametros.faixas, empresas)
    emissao = Emissao(fatura_id, competencia, empresas, valor, linhas)

    async def emitir() -> None:
        await _emitir(conexao, tenant_id, hoje, parametros, gateway, assinatura, pagador, emissao)

    if not await _no_gateway(resultado, AVISO_GATEWAY_EMISSAO, tenant_id, conexao, emitir):
        return resultado
    if situacao == "teste":
        await _mudar_situacao(conexao, tenant_id, "teste", "ativa")
        resultado.situacao = "ativa"
    resultado.fatura_emitida = fatura_id
    return resultado


@dataclass(frozen=True)
class Emissao:
    fatura_id: UUID
    competencia: date
    empresas: int
    valor: int
    linhas: list[Any]


async def _emitir(
    conexao: Any,
    tenant_id: UUID,
    hoje: date,
    parametros: Parametros,
    gateway: Gateway,
    assinatura: Any,
    pagador: Pagador | None,
    emissao: Emissao,
) -> None:
    """Cliente no gateway (criado ou atualizado), cobranca e a fatura gravada."""
    fatura_id, competencia, empresas = emissao.fatura_id, emissao.competencia, emissao.empresas
    valor, linhas = emissao.valor, emissao.linhas
    cliente_id = assinatura["gateway_cliente_id"]
    if cliente_id is None:
        nome = await conexao.fetchval("SELECT nome FROM tenant")
        cliente_id = await gateway.criar_cliente(str(tenant_id), nome, pagador)
        await conexao.execute(
            "UPDATE assinatura SET gateway_cliente_id = $1, pagador_sincronizado = $2,"
            " atualizada_em = now()",
            cliente_id,
            pagador is not None,
        )
    elif pagador is not None and not assinatura["pagador_sincronizado"]:
        # O pagador mudou na tela: o boleto novo ja sai com os dados novos.
        await gateway.atualizar_cliente(cliente_id, pagador)
        await conexao.execute(
            "UPDATE assinatura SET pagador_sincronizado = true, atualizada_em = now()"
        )
    vencimento = hoje + timedelta(days=parametros.dias_vencimento)
    cobranca = await gateway.criar_cobranca(
        cliente_id,
        str(fatura_id),
        valor,
        vencimento,
        f"CarbonLedger {competencia:%m/%Y}: {empresas} empresa(s) ativa(s)",
    )
    detalhamento = [linha.__dict__ for linha in linhas]
    await conexao.execute(
        "INSERT INTO fatura (tenant_id, fatura_id, competencia, empresas_ativas,"
        " valor_centavos, detalhamento, vencimento, gateway, gateway_cobranca_id,"
        " url_pagamento) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10)",
        tenant_id,
        fatura_id,
        competencia,
        empresas,
        valor,
        json.dumps(detalhamento),
        vencimento,
        gateway.nome,
        cobranca.cobranca_id,
        cobranca.url_pagamento,
    )
    await audit.registrar_asyncpg(
        conexao,
        tenant_id=tenant_id,
        acao="cobranca.fatura_emitida",
        entidade="fatura",
        entidade_id=fatura_id,
        dados={
            "competencia": competencia.isoformat(),
            "empresas_ativas": empresas,
            "valor_centavos": valor,
        },
    )


async def localizar(conexao: Any, gateway: str, cobranca_id: str) -> tuple[UUID, UUID] | None:
    """Tenant e fatura de uma cobranca do gateway (sem contexto: funcao do app_identidade)."""
    linha = await conexao.fetchrow(
        "SELECT tenant_id, fatura_id FROM cobranca_localizar($1, $2)", gateway, cobranca_id
    )
    return None if linha is None else (linha["tenant_id"], linha["fatura_id"])


async def registrar_evento(
    conexao: Any, tenant_id: UUID, fatura_id: UUID, gateway: str, evento: Evento
) -> bool:
    """Processa um evento do gateway uma vez so. Devolve False se ja tinha sido processado."""
    novo = await conexao.fetchval(
        "INSERT INTO cobranca_evento (tenant_id, evento_id, gateway, tipo, fatura_id)"
        " VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING RETURNING evento_id",
        tenant_id,
        evento.evento_id,
        gateway,
        evento.tipo,
        fatura_id,
    )
    if novo is None:
        return False
    if evento.tipo != EVENTO_PAGO:
        return True
    await _marcar_paga(conexao, tenant_id, fatura_id, {"gateway": gateway})
    return True


async def salvar_pagador(conexao: Any, tenant_id: UUID, usuario_id: UUID, pagador: Pagador) -> None:
    """Grava o pagador (a assinatura ja existe) e marca para atualizar no gateway."""
    await conexao.execute(
        "UPDATE assinatura SET pagador_nome = $1, pagador_cnpj = $2, pagador_email = $3,"
        " pagador_sincronizado = false, atualizada_em = now()",
        pagador.nome,
        pagador.cnpj,
        pagador.email,
    )
    # Sem os dados na trilha: o e-mail de cobranca pode ser de uma pessoa.
    await audit.registrar_asyncpg(
        conexao,
        tenant_id=tenant_id,
        usuario_id=usuario_id,
        acao="cobranca.pagador_alterado",
        entidade="assinatura",
        entidade_id=tenant_id,
        dados={},
    )


def parametros_da_configuracao(configuracao: Any) -> Parametros | None:
    if not configuracao.cobranca_tabela:
        return None
    return Parametros(
        faixas=precos.ler_tabela(configuracao.cobranca_tabela),
        dias_teste=configuracao.cobranca_dias_teste,
        dias_vencimento=configuracao.cobranca_dias_vencimento,
        dias_tolerancia=configuracao.cobranca_dias_tolerancia,
    )


def gateway_da_configuracao(configuracao: Any) -> Gateway | None:
    if not configuracao.cobranca_tabela or configuracao.cobranca_webhook_token is None:
        return None
    if configuracao.cobranca_gateway == "simulado":
        from app.modules.cobranca.gateway import GatewaySimulado

        return GatewaySimulado(
            configuracao.app_url_base, configuracao.cobranca_webhook_token.get_secret_value()
        )
    from app.modules.cobranca.asaas import GatewayAsaas

    chave = configuracao.asaas_api_key
    if chave is None:
        return None
    return GatewayAsaas(
        configuracao.asaas_url,
        chave.get_secret_value(),
        configuracao.cobranca_webhook_token.get_secret_value(),
    )
