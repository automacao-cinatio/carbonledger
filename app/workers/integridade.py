"""Tarefas de integridade: verificacao por tenant e o disparo diario do beat.

O disparo e a unica tarefa global: lista os tenants pela funcao identidade_tenants() (sem
contexto, so ids) e enfileira uma verificacao por tenant, com tenant_id explicito. Ele nao le
nem grava dado de cliente.
"""

import asyncio
from typing import Any

import asyncpg
from celery.schedules import crontab

from app.core.armazenamento import ArmazemS3
from app.core.config import obter_configuracao
from app.core.db import dsn_asyncpg
from app.core.tenancy import ContextoTenant
from app.modules.integridade import servico
from app.workers.celery_app import celery_app
from app.workers.registro import TAREFAS_GLOBAIS
from app.workers.tarefas import tarefa_de_tenant

NOME_DISPARO = "integridade.disparar"
TAREFAS_GLOBAIS[NOME_DISPARO] = (
    "beat diario: lista os tenants (so ids, por identidade_tenants) e enfileira"
    " integridade.verificar_tenant para cada um; nao toca dado de tenant"
)


@tarefa_de_tenant("integridade.verificar_tenant")
async def verificar_tenant(
    conexao: Any, contexto: ContextoTenant, *, origem: str = "agendada"
) -> dict[str, Any]:
    relatorio = await servico.verificar(
        conexao,
        contexto.tenant_id,
        ArmazemS3(obter_configuracao()),
        origem=origem,
        pedida_por=contexto.usuario_id,
    )
    return {"integra": relatorio.integra, "falhas": len(relatorio.falhas)}


async def _tenants() -> list[str]:
    conexao = await asyncpg.connect(dsn_asyncpg(obter_configuracao().database_url))
    try:
        return [str(linha[0]) for linha in await conexao.fetch("SELECT identidade_tenants()")]
    finally:
        await conexao.close()


@celery_app.task(name=NOME_DISPARO)  # type: ignore[untyped-decorator]
def disparar() -> int:
    tenants = asyncio.run(_tenants())
    for tenant_id in tenants:
        verificar_tenant.delay(tenant_id=tenant_id)
    return len(tenants)


celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "integridade-diaria": {"task": NOME_DISPARO, "schedule": crontab(hour=3, minute=0)},
}
