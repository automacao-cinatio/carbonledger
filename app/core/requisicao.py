"""Middleware que gera o request_id e o coloca no contexto dos logs."""

import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

CABECALHO_REQUEST_ID = "X-Request-ID"


class MiddlewareRequestId(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        log = structlog.get_logger()
        resposta = await call_next(request)
        resposta.headers[CABECALHO_REQUEST_ID] = request_id
        log.info(
            "requisicao",
            metodo=request.method,
            caminho=request.url.path,
            status=resposta.status_code,
        )
        return resposta
