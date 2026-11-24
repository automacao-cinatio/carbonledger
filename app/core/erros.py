"""Erros comuns. Acesso negado responde 404, nunca 403.

Um 403 confirma que o recurso existe; o 404 nao entrega isso a quem nao tem acesso.
Navegador pedindo tela (GET com Accept text/html, fora de /api e fora do HTMX) recebe a
pagina de erro; o resto recebe JSON.
"""

from fastapi import FastAPI, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

CORPO_NAO_ENCONTRADO = {"detalhe": "Não encontrado."}


class AcessoNegado(Exception):
    """Sem contexto, sem permissao ou recurso de outro tenant: a resposta e sempre 404."""


class MuitasTentativas(Exception):
    """Limite de tentativas de login passado: responde 429 dizendo quanto esperar."""

    def __init__(self, minutos: int) -> None:
        super().__init__(f"espere {minutos} minutos")
        self.minutos = minutos


class ErroValidacao(Exception):
    """Dado recusado por regra de negocio. A mensagem diz como corrigir e vai para a tela."""

    def __init__(self, campo: str, mensagem: str) -> None:
        super().__init__(mensagem)
        self.campo = campo
        self.mensagem = mensagem


class ErrosDeValidacao(Exception):
    """Varios campos recusados de uma vez: a pessoa ve todos os problemas no mesmo envio."""

    def __init__(self, erros: list[ErroValidacao]) -> None:
        super().__init__("; ".join(e.mensagem for e in erros))
        self.erros = erros


class ColetorDeErros:
    """Junta os erros de um formulario e levanta todos no fim (se houver)."""

    def __init__(self) -> None:
        self.erros: list[ErroValidacao] = []

    def adicionar(self, campo: str, mensagem: str) -> None:
        self.erros.append(ErroValidacao(campo, mensagem))

    def tem(self, campo: str) -> bool:
        return any(e.campo == campo for e in self.erros)

    def levantar_se_houver(self) -> None:
        if self.erros:
            raise ErrosDeValidacao(self.erros)


def quer_pagina(request: Request) -> bool:
    return (
        request.method == "GET"
        and "text/html" in request.headers.get("accept", "")
        and not request.url.path.startswith("/api")
        and "hx-request" not in request.headers
    )


async def _nao_encontrado(request: Request) -> Response:
    if quer_pagina(request):
        # Import tardio: o modulo web depende da configuracao, nao o contrario.
        from app.web.portal import montar_portal, renderizar

        if getattr(request.state, "sessao", None) is None:
            # Sem sessao (ou sessao expirada), qualquer tela leva ao login: nada vaza.
            return RedirectResponse("/entrar", status_code=status.HTTP_303_SEE_OTHER)

        return renderizar(
            request,
            "paginas/erro.html",
            await montar_portal(request),
            status_code=status.HTTP_404_NOT_FOUND,
            titulo="Página não encontrada",
            mensagem="O endereço não existe ou você não tem acesso a ele.",
        )
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content=CORPO_NAO_ENCONTRADO)


async def _tratar_acesso_negado(request: Request, _erro: Exception) -> Response:
    return await _nao_encontrado(request)


async def _tratar_validacao(_request: Request, erro: Exception) -> JSONResponse:
    erros = erro.erros if isinstance(erro, ErrosDeValidacao) else [erro]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "erros": [
                {"campo": e.campo, "mensagem": e.mensagem}
                for e in erros
                if isinstance(e, ErroValidacao)
            ]
        },
    )


async def _tratar_http(request: Request, erro: Exception) -> Response:
    if isinstance(erro, StarletteHTTPException) and erro.status_code in (
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    ):
        return await _nao_encontrado(request)
    return await http_exception_handler(request, erro)  # type: ignore[arg-type]


async def _tratar_muitas_tentativas(request: Request, erro: Exception) -> Response:
    minutos = erro.minutos if isinstance(erro, MuitasTentativas) else 10
    mensagem = f"Muitas tentativas de entrada. Espere {minutos} minutos e tente de novo."
    if quer_pagina(request):
        from app.web.portal import montar_portal, renderizar

        return renderizar(
            request,
            "paginas/erro.html",
            await montar_portal(request),
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            titulo="Muitas tentativas",
            mensagem=mensagem,
        )
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS, content={"detalhe": mensagem}
    )


def registrar_tratadores(app: FastAPI) -> None:
    app.add_exception_handler(MuitasTentativas, _tratar_muitas_tentativas)
    app.add_exception_handler(AcessoNegado, _tratar_acesso_negado)
    app.add_exception_handler(ErroValidacao, _tratar_validacao)
    app.add_exception_handler(ErrosDeValidacao, _tratar_validacao)
    app.add_exception_handler(StarletteHTTPException, _tratar_http)
