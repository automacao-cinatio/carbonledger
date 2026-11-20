"""Cabecalhos de seguranca em toda resposta, com Content-Security-Policy estrita.

Nada de dominio externo, script inline, estilo inline ou eval. O portal so carrega o que o
proprio app serve em /static.
"""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        # data: so para os icones SVG que o CSS do Tabler embute.
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    ]
)

CABECALHOS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "x-frame-options": "DENY",
    "cross-origin-opener-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
}

HSTS = "max-age=63072000; includeSubDomains"


class MiddlewareCabecalhos:
    """ASGI puro: acrescenta os cabecalhos sem tocar no corpo da resposta."""

    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.cabecalhos = [(k.encode(), v.encode()) for k, v in CABECALHOS.items()]
        if hsts:
            self.cabecalhos.append((b"strict-transport-security", HSTS.encode()))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def enviar(mensagem: Message) -> None:
            if mensagem["type"] == "http.response.start":
                existentes = {nome.lower() for nome, _ in mensagem.get("headers", [])}
                mensagem["headers"] = list(mensagem.get("headers", [])) + [
                    (nome, valor) for nome, valor in self.cabecalhos if nome not in existentes
                ]
            await send(mensagem)

        await self.app(scope, receive, enviar)
