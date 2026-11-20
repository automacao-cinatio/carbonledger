"""Pagina /ui: galeria das macros do portal, so fora de producao.

Os exemplos usam dados ficticios. As rotas /ui/* mostram o padrao das acoes HTMX: validacao
de campo (devolve a mensagem), envio com erro por campo (422) e acao com aviso (204).
"""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse

from app.api.paginas import resposta_com_aviso
from app.core.config import obter_configuracao
from app.core.erros import AcessoNegado, ErroValidacao
from app.modules.organizacao.acessos import normalizar_email
from app.web.formatos import NumeroInvalido, ler_numero
from app.web.portal import EmpresaVisivel, Inventario, Portal, montar_portal, renderizar
from app.web.renderizacao import templates

router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)

COLUNAS_EXEMPLO = [
    {"chave": "escopo", "titulo": "Escopo"},
    {"chave": "emissoes", "titulo": "Emissões", "unidade": "tCO₂e", "casas": 2},
    {"chave": "participacao", "titulo": "Participação", "unidade": "%", "casas": 1},
]
LINHAS_EXEMPLO = [
    {"escopo": "Escopo 1", "emissoes": Decimal("1234.567"), "participacao": Decimal("61.4")},
    {"escopo": "Escopo 2", "emissoes": Decimal("512.3"), "participacao": Decimal("25.5")},
    {"escopo": "Escopo 3", "emissoes": Decimal("263.05"), "participacao": Decimal("13.1")},
]


def _so_fora_de_producao() -> None:
    if obter_configuracao().em_producao:
        raise AcessoNegado


def _grafico_exemplo() -> dict[str, object]:
    # Valores como texto: o Decimal nao vira float no Python; o ECharts le o numero.
    return {
        "xAxis": {"type": "category", "data": [linha["escopo"] for linha in LINHAS_EXEMPLO]},
        "yAxis": {"type": "value", "name": "tCO₂e"},
        "series": [
            {
                "type": "bar",
                "name": "Emissões",
                "data": [str(linha["emissoes"]) for linha in LINHAS_EXEMPLO],
            }
        ],
    }


@router.get("", response_class=HTMLResponse)
async def galeria(request: Request) -> Response:
    _so_fora_de_producao()
    real = await montar_portal(request)
    portal = Portal(
        usuario_nome=real.usuario_nome or "Pessoa de exemplo",
        tenant_nome="Cliente de exemplo",
        acessos=real.acessos,
        empresas=[
            EmpresaVisivel("e1", "Empresa Exemplo Ltda"),
            EmpresaVisivel("e2", "Filial Norte"),
        ],
        empresa_atual="e1",
        inventario=Inventario(ano=2026, estado="Em coleta", coleta=(12, 40), evidencia=(5, 40)),
        csrf=real.csrf,
    )
    return renderizar(
        request,
        "paginas/ui.html",
        portal,
        pagina_atual="/ui",
        colunas_exemplo=COLUNAS_EXEMPLO,
        linhas_exemplo=LINHAS_EXEMPLO,
        grafico_exemplo=_grafico_exemplo(),
        fator_exemplo=Decimal("2.6012"),
    )


def _erro_consumo(valor: str) -> str | None:
    if not valor.strip():
        return "Informe o consumo do mês. Se não houve consumo, digite 0."
    try:
        numero = ler_numero(valor)
    except NumeroInvalido:
        return "Use só números, com vírgula para os decimais, como 1.234,5."
    if numero < 0:
        return "O consumo não pode ser negativo. Para corrigir um lançamento, use o estorno."
    return None


def _erro_responsavel(valor: str) -> str | None:
    try:
        normalizar_email(valor)
    except ErroValidacao as erro:
        return erro.mensagem
    return None


VALIDADORES = {"consumo": _erro_consumo, "responsavel": _erro_responsavel}


@router.post("/validar/{campo}", response_class=HTMLResponse)
async def validar_campo(request: Request, campo: str) -> Response:
    _so_fora_de_producao()
    validador = VALIDADORES.get(campo)
    if validador is None:
        raise AcessoNegado
    formulario = await request.form()
    valor = str(formulario.get(campo, ""))
    return templates.TemplateResponse(
        request, "partes/mensagem_campo.html", {"nome": campo, "erro": validador(valor)}
    )


@router.post("/enviar")
async def enviar(
    consumo: Annotated[str, Form()] = "",
    responsavel: Annotated[str, Form()] = "",
) -> Response:
    _so_fora_de_producao()
    for campo, erro in (
        ("responsavel", _erro_responsavel(responsavel)),
        ("consumo", _erro_consumo(consumo)),
    ):
        if erro:
            raise ErroValidacao(campo, erro)
    return resposta_com_aviso("Consumo de exemplo salvo (nada foi gravado).")


@router.post("/acao")
async def acao_de_exemplo() -> Response:
    _so_fora_de_producao()
    return resposta_com_aviso("Ação de exemplo concluída (nada foi alterado).")
