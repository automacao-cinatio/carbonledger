"""Tarefas de cobranca: a rodada diaria por tenant e o disparo do beat (P22).

O disparo e global: lista os tenants pela funcao identidade_tenants() (so ids) e enfileira
cobranca.rodada_tenant com tenant_id explicito. Sem COBRANCA_TABELA, nada roda.
"""

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg
from celery.schedules import crontab

from app.core.config import obter_configuracao
from app.core.db import dsn_asyncpg
from app.core.tenancy import ContextoTenant
from app.modules.cobranca import servico
from app.workers.celery_app import celery_app
from app.workers.registro import TAREFAS_GLOBAIS
from app.workers.tarefas import tarefa_de_tenant

FUSO = ZoneInfo("America/Sao_Paulo")
NOME_DISPARO = "cobranca.disparar"
TAREFAS_GLOBAIS[NOME_DISPARO] = (
    "beat diario: lista os tenants (so ids, por identidade_tenants) e enfileira"
    " cobranca.rodada_tenant para cada um; nao toca dado de tenant"
)


@tarefa_de_tenant("cobranca.rodada_tenant")
async def rodada_tenant(conexao: Any, contexto: ContextoTenant) -> dict[str, Any]:
    configuracao = obter_configuracao()
    parametros = servico.parametros_da_configuracao(configuracao)
    gateway = servico.gateway_da_configuracao(configuracao)
    if parametros is None or gateway is None:
        return {"cobranca": "desligada"}
    resultado = await servico.rodada(
        conexao, contexto.tenant_id, datetime.now(FUSO).date(), parametros, gateway
    )
    return {
        "situacao": resultado.situacao,
        "fatura": str(resultado.fatura_emitida) if resultado.fatura_emitida else None,
        "vencidas": resultado.vencidas,
    }


async def _tenants() -> list[str]:
    conexao = await asyncpg.connect(dsn_asyncpg(obter_configuracao().database_url))
    try:
        return [str(linha[0]) for linha in await conexao.fetch("SELECT identidade_tenants()")]
    finally:
        await conexao.close()


@celery_app.task(name=NOME_DISPARO)  # type: ignore[untyped-decorator]
def disparar() -> int:
    if not obter_configuracao().cobranca_tabela:
        return 0
    tenants = asyncio.run(_tenants())
    for tenant_id in tenants:
        rodada_tenant.delay(tenant_id=tenant_id)
    return len(tenants)


celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "cobranca-diaria": {"task": NOME_DISPARO, "schedule": crontab(hour=6, minute=0)},
}
