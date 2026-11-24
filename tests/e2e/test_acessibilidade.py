"""Acessibilidade e CSP num navegador de verdade (Playwright + axe-core).

Sobe o app num servidor local, abre as telas, roda o axe-core com as regras WCAG 2.2 A e AA
e exercita as interacoes do portal. Qualquer erro no console (inclusive violacao de CSP)
reprova a tela.
"""

import os
from collections.abc import Iterator
from typing import Any

import pytest

from app.main import criar_app
from tests.acessibilidade import porta_livre, servir, vigiar_console, violacoes_axe

pytestmark = pytest.mark.e2e

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def servidor() -> Iterator[str]:
    yield from servir(criar_app(), porta_livre())


@pytest.fixture(scope="module")
def navegador() -> Iterator[Any]:
    with sync_api.sync_playwright() as playwright:
        try:
            navegador = playwright.chromium.launch()
        except Exception as erro:  # navegador nao instalado
            if os.environ.get("CI"):
                raise
            pytest.skip(
                f"Chromium do Playwright ausente: uv run playwright install chromium ({erro})"
            )
        yield navegador
        navegador.close()


@pytest.fixture
def pagina(navegador: Any) -> Iterator[Any]:
    contexto = navegador.new_context(locale="pt-BR")
    pagina = contexto.new_page()
    pagina.erros_console = vigiar_console(pagina)  # type: ignore[attr-defined]
    yield pagina
    contexto.close()


def _sem_erros(pagina: Any, *, documento_404: bool = False) -> None:
    erros = list(pagina.erros_console)
    if documento_404:
        # A propria pagina de erro chega com 404: e o esperado, nao um recurso faltando.
        primeiro = next((e for e in erros if "status of 404" in e), None)
        if primeiro is not None:
            erros.remove(primeiro)
    assert not erros, erros


@pytest.mark.parametrize("caminho", ["/ui", "/entrar", "/nao-existe"])
def test_telas_sem_violacao_wcag_nem_de_csp(servidor: str, pagina: Any, caminho: str) -> None:
    pagina.goto(servidor + caminho, wait_until="networkidle")
    _sem_erros(pagina, documento_404=caminho == "/nao-existe")
    assert pagina.locator("html").get_attribute("lang") == "pt-BR"
    violacoes = violacoes_axe(pagina)
    assert not violacoes, violacoes


def test_interacoes_do_portal(servidor: str, pagina: Any) -> None:
    pagina.goto(servidor + "/ui", wait_until="networkidle")

    # Grafico desenhado pelo ECharts (sem eval nem estilo inline bloqueado).
    pagina.wait_for_selector("#grafico-exemplo[data-desenhado='1'] canvas")

    # Validacao na hora: a mensagem diz como corrigir e o campo fica marcado.
    consumo = pagina.locator("#campo-consumo")
    consumo.fill("1.5")
    consumo.blur()
    pagina.wait_for_function(
        "() => document.getElementById('erro-consumo').textContent.includes('vírgula')"
    )
    assert consumo.get_attribute("aria-invalid") == "true"
    consumo.fill("1.234,5")
    consumo.blur()
    pagina.wait_for_function(
        "() => document.getElementById('erro-consumo').textContent.trim() === ''"
    )
    assert consumo.get_attribute("aria-invalid") is None

    # Rascunho: o que foi digitado volta depois de recarregar a pagina.
    pagina.locator("#campo-responsavel").fill("pessoa@empresa.com.br")
    pagina.reload(wait_until="networkidle")
    assert pagina.locator("#campo-responsavel").input_value() == "pessoa@empresa.com.br"
    assert pagina.locator("#campo-consumo").input_value() == "1.234,5"

    # Envio por HTMX: aviso na regiao aria-live e rascunho apagado.
    pagina.get_by_role("button", name="Salvar consumo").click()
    aviso = pagina.locator("#cl-aviso")
    aviso.get_by_text("Consumo de exemplo salvo").wait_for()
    assert aviso.get_attribute("aria-live") == "polite"
    pagina.reload(wait_until="networkidle")
    assert pagina.locator("#campo-responsavel").input_value() == ""

    # Acao irreversivel: so libera depois de digitar o texto exigido; teclado basta.
    pagina.get_by_role("button", name="Fechar inventário de exemplo").focus()
    pagina.keyboard.press("Enter")
    confirmar = pagina.locator("#botao-fechar-exemplo")
    confirmar.wait_for(state="visible")
    assert confirmar.is_disabled()
    entrada = pagina.locator("#confirmar-fechar-exemplo")
    entrada.fill("Empresa Exemplo")
    assert confirmar.is_disabled()
    entrada.fill("Empresa Exemplo Ltda")
    assert confirmar.is_enabled()
    confirmar.click()
    aviso.get_by_text("Ação de exemplo concluída").wait_for()
    confirmar.wait_for(state="hidden")

    # Glossario: a definicao aparece ao focar o botao de ajuda.
    pagina.get_by_role("button", name="O que é CO₂e?").focus()
    pagina.get_by_text("Dióxido de carbono equivalente").wait_for()

    _sem_erros(pagina)
