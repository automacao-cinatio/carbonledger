"""App Celery. Toda tarefa futura recebe tenant_id explicito e usa asyncpg direto."""

from celery import Celery

from app.core.config import obter_configuracao

_configuracao = obter_configuracao()

celery_app = Celery(
    "carbonledger",
    broker=_configuracao.redis_url,
    backend=_configuracao.redis_url,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="America/Sao_Paulo",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
)
