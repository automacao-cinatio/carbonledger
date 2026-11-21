"""Acessibilidade das telas com sessao: servidor real, login no navegador e axe-core.

Usa o Postgres, o Redis e o provedor OIDC simulados da suite de isolamento. Cada tela do
portal com cliente aberto passa pelo axe-core (WCAG 2.2 A e AA) e nao pode ter erro de
console (inclusive de CSP).
"""

import asyncio
import json
import os
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.cli import criar_tenant
from app.core.config import obter_configuracao
from app.main import criar_app
from tests.acessibilidade import porta_livre, servir, vigiar_console, violacoes_axe
from tests.isolation.conftest import Banco

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def portal(banco: Banco, redis_url: str, emissor_oidc: str) -> Iterator[str]:
    porta = porta_livre()
    ambiente = {
        "DATABASE_URL": banco.dsn_app,
        "REDIS_URL": redis_url,
        "OIDC_DEV_EMISSOR": emissor_oidc,
        "COOKIE_SEGURO": "false",
        "APP_URL_BASE": f"http://127.0.0.1:{porta}",
    }
    anteriores = {nome: os.environ.get(nome) for nome in ambiente}
    os.environ.update(ambiente)
    obter_configuracao.cache_clear()
    try:
        yield from servir(criar_app(), porta)
    finally:
        for nome, valor in anteriores.items():
            if valor is None:
                os.environ.pop(nome, None)
            else:
                os.environ[nome] = valor
        obter_configuracao.cache_clear()


async def _estrutura(banco: Banco, tenant_id: UUID) -> tuple[UUID, UUID]:
    grupo, empresa, unidade = uuid4(), uuid4(), uuid4()
    admin = await asyncpg.connect(banco.dsn_admin)
    try:
        await admin.execute(
            "INSERT INTO grupo (tenant_id, grupo_id, nome) VALUES ($1, $2, 'Grupo Acessivel')",
            tenant_id,
            grupo,
        )
        await admin.execute(
            "INSERT INTO empresa (tenant_id, empresa_id, grupo_id, nome, cnpj, uf, municipio)"
            " VALUES ($1, $2, $3, 'Empresa Acessivel S.A.', '11222333000181', 'MG',"
            " 'Belo Horizonte')",
            tenant_id,
            empresa,
            grupo,
        )
        await admin.execute(
            "INSERT INTO unidade (tenant_id, unidade_id, empresa_id, nome, uf, municipio,"
            " participacao_societaria, controle_operacional, controle_financeiro)"
            " VALUES ($1, $2, $3, 'Fabrica Contagem', 'MG', 'Contagem', 75, true, false)",
            tenant_id,
            unidade,
            empresa,
        )
    finally:
        await admin.close()
    return empresa, unidade


@pytest.fixture(scope="module")
def pagina_logada(portal: str, banco: Banco) -> Iterator[tuple[Any, UUID, UUID]]:
    tenant_id, token = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Acessivel", "pessoa@acessivel.example")
    )
    empresa, unidade = asyncio.run(_estrutura(banco, tenant_id))
    with sync_api.sync_playwright() as playwright:
        try:
            navegador = playwright.chromium.launch()
        except Exception as erro:
            if os.environ.get("CI"):
                raise
            pytest.skip(
                f"Chromium do Playwright ausente: uv run playwright install chromium ({erro})"
            )
        contexto = navegador.new_context(locale="pt-BR")
        pagina = contexto.new_page()
        # Login de verdade: o navegador passa pela tela do provedor simulado.
        pagina.goto(f"{portal}/convite/{token}/dev")
        pagina.fill("input[name=username]", "pessoa-acessivel")
        pagina.fill(
            "textarea[name=claims]",
            json.dumps(
                {"email": "pessoa@acessivel.example", "email_verified": True, "name": "Pessoa"}
            ),
        )
        pagina.click("input[type=submit]")
        # Folga maior: o login passa por dois servidores e a maquina pode estar carregada.
        pagina.wait_for_url(f"{portal}/", timeout=60_000)
        yield pagina, empresa, unidade
        navegador.close()


TELAS = [
    "/",
    "/clientes",
    "/organizacao",
    "/organizacao/grupos/novo",
    "/organizacao/empresas/nova",
    "/organizacao/empresas/{empresa}",
    "/organizacao/empresas/{empresa}/editar",
    "/organizacao/empresas/{empresa}/unidades/nova",
    "/organizacao/unidades/{unidade}/editar",
    "/organizacao/empresas/00000000-0000-4000-8000-000000000000",
]


@pytest.mark.parametrize("modelo", TELAS)
def test_telas_logadas_sem_violacao(
    portal: str, pagina_logada: tuple[Any, UUID, UUID], modelo: str
) -> None:
    pagina, empresa, unidade = pagina_logada
    erros = vigiar_console(pagina)
    caminho = modelo.format(empresa=empresa, unidade=unidade)
    pagina.goto(portal + caminho, wait_until="networkidle")
    if caminho.endswith("000000000000"):
        assert pagina.get_by_text("Página não encontrada").is_visible()
        erros = [e for e in erros if "status of 404" not in e]
    assert not erros, erros
    violacoes = violacoes_axe(pagina)
    assert not violacoes, violacoes


def test_formulario_mostra_todos_os_erros(
    portal: str, pagina_logada: tuple[Any, UUID, UUID]
) -> None:
    pagina, _empresa, _unidade = pagina_logada
    pagina.goto(portal + "/organizacao/empresas/nova", wait_until="networkidle")
    pagina.fill("#campo-cnpj", "11.222.333/0001-82")
    pagina.get_by_role("button", name="Salvar empresa").click()
    pagina.get_by_text("Corrija os").wait_for()
    for campo in ("cnpj", "nome", "municipio"):
        assert pagina.locator(f"#campo-{campo}").get_attribute("aria-invalid") == "true", campo
    assert "dígitos finais" in pagina.locator("#erro-cnpj").text_content()
    assert not violacoes_axe(pagina)
