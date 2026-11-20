"""Erros comuns da API. Acesso negado responde 404, nunca 403.

Um 403 confirma que o recurso existe; o 404 nao entrega isso a quem nao tem acesso.
"""

from fastapi import FastAPI, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

CORPO_NAO_ENCONTRADO = {"detalhe": "Não encontrado."}


class AcessoNegado(Exception):
    """Sem contexto, sem permissao ou recurso de outro tenant: a resposta e sempre 404."""


def _nao_encontrado() -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content=CORPO_NAO_ENCONTRADO)


async def _tratar_acesso_negado(_request: Request, _erro: Exception) -> JSONResponse:
    return _nao_encontrado()


async def _tratar_http(request: Request, erro: Exception) -> JSONResponse:
    if isinstance(erro, StarletteHTTPException) and erro.status_code in (
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    ):
        return _nao_encontrado()
    resposta = await http_exception_handler(request, erro)  # type: ignore[arg-type]
    assert isinstance(resposta, JSONResponse)  # noqa: S101  # nosec B101
    return resposta


def registrar_tratadores(app: FastAPI) -> None:
    app.add_exception_handler(AcessoNegado, _tratar_acesso_negado)
    app.add_exception_handler(StarletteHTTPException, _tratar_http)
