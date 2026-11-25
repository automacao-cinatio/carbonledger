"""App Celery. Toda tarefa recebe tenant_id explicito e usa asyncpg direto.

A checagem acontece na publicacao (send_task, por onde passam delay e apply_async), para o
erro aparecer em quem enfileira, e de novo na execucao (tarefa_de_tenant).
"""

from collections.abc import Mapping, Sequence
from typing import Any

from celery import Celery
from celery.result import AsyncResult
from celery.signals import setup_logging

from app.core.config import obter_configuracao
from app.core.logs import configurar_logs
from app.workers.registro import exigir_tenant


class CeleryCarbonLedger(Celery):  # type: ignore[misc]
    def send_task(
        self,
        name: str,
        args: Sequence[Any] | None = None,
        kwargs: Mapping[str, Any] | None = None,
        **opcoes: Any,
    ) -> AsyncResult:
        exigir_tenant(name, args, kwargs)
        return super().send_task(name, args=args, kwargs=kwargs, **opcoes)


_configuracao = obter_configuracao()

celery_app = CeleryCarbonLedger(
    "carbonledger",
    broker=_configuracao.redis_url,
    backend=_configuracao.redis_url,
    include=[
        "app.workers.tarefas",
        "app.workers.calculo",
        "app.workers.integridade",
        "app.workers.campanhas",
        "app.workers.cobranca",
    ],
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="America/Sao_Paulo",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
)


@setup_logging.connect
def _configurar_logs(**_: Any) -> None:
    """Logs do worker no mesmo formato JSON da API."""
    configurar_logs(obter_configuracao().log_nivel)
