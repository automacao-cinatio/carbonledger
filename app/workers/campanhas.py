"""Tarefas das campanhas de coleta: lembrete diario por tenant e o disparo do beat.

O disparo e global (justificado no registro): lista os tenants so pelos ids
(identidade_tenants) e enfileira campanhas.lembrar_tenant com tenant_id explicito.
"""

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from celery.schedules import crontab

from app.core import email as servico_email
from app.core.config import obter_configuracao
from app.core.tenancy import ContextoTenant
from app.modules.coleta import campanhas
from app.workers.celery_app import celery_app
from app.workers.integridade import _tenants
from app.workers.registro import TAREFAS_GLOBAIS
from app.workers.tarefas import tarefa_de_tenant

NOME_DISPARO = "campanhas.disparar"
TAREFAS_GLOBAIS[NOME_DISPARO] = (
    "beat diario: lista os tenants (so ids, por identidade_tenants) e enfileira"
    " campanhas.lembrar_tenant para cada um; nao toca dado de tenant"
)
FUSO = ZoneInfo("America/Sao_Paulo")


async def _enviar(para: str, assunto: str, corpo: str) -> None:
    await servico_email.enviar_email(obter_configuracao(), para, assunto, corpo)


@tarefa_de_tenant("campanhas.lembrar_tenant")
async def lembrar_tenant(conexao: Any, contexto: ContextoTenant) -> int:
    return await campanhas.lembrar(conexao, contexto.tenant_id, datetime.now(FUSO).date(), _enviar)


@celery_app.task(name=NOME_DISPARO)  # type: ignore[untyped-decorator]
def disparar() -> int:
    tenants = asyncio.run(_tenants())
    for tenant_id in tenants:
        lembrar_tenant.delay(tenant_id=tenant_id)
    return len(tenants)


celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "campanhas-diaria": {"task": NOME_DISPARO, "schedule": crontab(hour=8, minute=0)},
}
