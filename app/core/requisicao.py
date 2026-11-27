"""Middleware que gera o request_id e o coloca no contexto dos logs.

O endpoint roda em outra task (BaseHTTPMiddleware), entao o tenant_id ligado la nao volta
para ca pelos contextvars; o log da requisicao le o contexto de request.state.
duracao_ms e o tempo no servidor ate a resposta ficar pronta (sem a rede ate o navegador).
"""

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.tenancy import ATRIBUTO_REQUISICAO, ContextoTenant

CABECALHO_REQUEST_ID = "X-Request-ID"


class MiddlewareRequestId(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        log = structlog.get_logger()
        inicio = time.perf_counter()
        resposta = await call_next(request)
        duracao_ms = round((time.perf_counter() - inicio) * 1000, 1)
        resposta.headers[CABECALHO_REQUEST_ID] = request_id
        contexto = getattr(request.state, ATRIBUTO_REQUISICAO, None)
        log.info(
            "requisicao",
            metodo=request.method,
            caminho=request.url.path,
            status=resposta.status_code,
            duracao_ms=duracao_ms,
            tenant_id=str(contexto.tenant_id) if isinstance(contexto, ContextoTenant) else None,
        )
        return resposta
