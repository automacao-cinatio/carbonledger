"""Cobranca por empresa ativa por mes (P22): assinatura, faturas, vencimento e pagamento.

Roda com asyncpg, na transacao com o contexto do tenant (worker diario e webhook):
- assinatura nasce em teste, com fim em criado_em do tenant + COBRANCA_DIAS_TESTE;
- depois do teste, uma fatura por mes (competencia), com as empresas ativas no dia da emissao
  e o valor pela tabela progressiva; o id da fatura deriva do tenant e da competencia, e o
  gateway e idempotente por ele: repetir a rodada nunca cobra duas vezes;
- fatura aberta passa do vencimento: vencida; vencida ha mais que a tolerancia: assinatura
  inadimplente (por enquanto so aviso; o que bloquear e decisao pendente);
- pagamento confirmado pelo gateway: fatura paga e, sem outra vencida, assinatura ativa. Cada
  evento do gateway e processado uma vez so.
"""

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid5

from app.core import audit
from app.modules.cobranca import precos
from app.modules.cobranca.gateway import EVENTO_PAGO, Evento, Gateway, Pagador

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
    linha = await conexao.fetchrow("SELECT * FROM assinatura")
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
    return await conexao.fetchrow("SELECT * FROM assinatura")


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
    situacao = assinatura["situacao"]
    resultado = Rodada(situacao)
    if situacao == "cancelada":
        return resultado
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
    if situacao == "teste":
        await _mudar_situacao(conexao, tenant_id, "teste", "ativa")
        resultado.situacao = "ativa"
    resultado.fatura_emitida = fatura_id
    return resultado


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
            dados={"gateway": gateway},
        )
    situacao = await conexao.fetchval("SELECT situacao FROM assinatura")
    pendente = await conexao.fetchval("SELECT count(*) FROM fatura WHERE situacao = 'vencida'")
    if situacao == "inadimplente" and not pendente:
        await _mudar_situacao(conexao, tenant_id, "inadimplente", "ativa")
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
