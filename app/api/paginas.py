"""Telas do portal: entrar, inicio, escolha de cliente e selecao de empresa.

Sem sessao, as telas levam para /entrar; sem cliente escolhido, para /clientes.
As acoes sao HTMX: respondem 204 com o aviso no cabecalho X-Aviso e, quando a tela inteira
muda, HX-Redirect ou HX-Refresh.
"""

from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.autenticacao import auditar_sessao
from app.core.erros import AcessoNegado
from app.core.security.acesso import resolver_acesso
from app.core.security.oidc import Provedor
from app.core.security.sessao import ArmazemSessoes, Sessao
from app.web.portal import empresa_visivel, montar_portal, renderizar

router = APIRouter(tags=["paginas"], include_in_schema=False)

APRESENTACAO_PROVEDORES = {
    "google": ("Google", "brand-google"),
    "microsoft": ("Microsoft", "brand-windows"),
    "dev": ("o provedor de desenvolvimento", "code"),
}


def resposta_com_aviso(aviso: str, **cabecalhos: str) -> Response:
    """204 com o texto do aviso (codificado: cabecalho HTTP nao leva acento)."""
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"X-Aviso": quote(aviso), **cabecalhos},
    )


def _sessao(request: Request) -> Sessao | None:
    sessao = getattr(request.state, "sessao", None)
    return sessao if isinstance(sessao, Sessao) else None


@router.get("/entrar", response_class=HTMLResponse)
async def tela_entrar(request: Request) -> Response:
    if _sessao(request) is not None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    provedores: dict[str, Provedor] = request.app.state.provedores
    opcoes = [
        {
            "nome": nome,
            "rotulo": APRESENTACAO_PROVEDORES[nome][0],
            "icone": APRESENTACAO_PROVEDORES[nome][1],
        }
        for nome in provedores
        if nome in APRESENTACAO_PROVEDORES
    ]
    return renderizar(
        request, "paginas/entrar.html", await montar_portal(request), provedores=opcoes
    )


@router.get("/", response_class=HTMLResponse)
async def tela_inicio(request: Request) -> Response:
    if _sessao(request) is None:
        return RedirectResponse("/entrar", status_code=status.HTTP_303_SEE_OTHER)
    if getattr(request.state, "acesso", None) is None:
        return RedirectResponse("/clientes", status_code=status.HTTP_303_SEE_OTHER)
    return renderizar(
        request, "paginas/inicio.html", await montar_portal(request), pagina_atual="/"
    )


@router.get("/clientes", response_class=HTMLResponse)
async def tela_clientes(request: Request) -> Response:
    if _sessao(request) is None:
        return RedirectResponse("/entrar", status_code=status.HTTP_303_SEE_OTHER)
    return renderizar(request, "paginas/clientes.html", await montar_portal(request))


@router.post("/clientes/{tenant_id}")
async def abrir_cliente(request: Request, tenant_id: UUID) -> Response:
    sessao = _sessao(request)
    if sessao is None:
        raise AcessoNegado
    acesso = await resolver_acesso(request.app.state.fabrica_sessoes, sessao.usuario_id, tenant_id)
    if acesso is None:
        raise AcessoNegado
    armazem: ArmazemSessoes = request.app.state.sessoes
    await armazem.trocar_tenant(sessao, tenant_id)
    await auditar_sessao(
        request, sessao.usuario_id, tenant_id, "sessao.entrou", {"origem": "troca_de_cliente"}
    )
    return resposta_com_aviso("Cliente aberto.", **{"HX-Redirect": "/"})


@router.post("/sessao/empresa")
async def escolher_empresa(request: Request, empresa_id: Annotated[UUID, Form()]) -> Response:
    sessao = _sessao(request)
    if sessao is None or getattr(request.state, "acesso", None) is None:
        raise AcessoNegado
    empresa = empresa_visivel(await montar_portal(request), empresa_id)
    if empresa is None:
        raise AcessoNegado
    armazem: ArmazemSessoes = request.app.state.sessoes
    await armazem.trocar_empresa(sessao, empresa_id)
    return resposta_com_aviso(f"Empresa em foco: {empresa.nome}.", **{"HX-Refresh": "true"})
