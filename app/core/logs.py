"""Logs estruturados (JSON) com request_id.

Regra: nada de dado pessoal em log. O tenant_id entra no contexto a partir do P03.
"""

import logging

import structlog


def configurar_logs(nivel: str = "INFO") -> None:
    nivel_numerico = logging.getLevelNamesMapping().get(nivel.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", level=nivel_numerico, force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(nivel_numerico),
        cache_logger_on_first_use=True,
    )
