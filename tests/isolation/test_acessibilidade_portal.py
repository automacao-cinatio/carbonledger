"""Acessibilidade das telas com sessao: servidor real, login no navegador e axe-core.

Usa o Postgres, o Redis e o provedor OIDC simulados da suite de isolamento. Cada tela do
portal com cliente aberto passa pelo axe-core (WCAG 2.2 A e AA) e nao pode ter erro de
console (inclusive de CSP).
"""

import asyncio
import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
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


async def _estrutura(banco: Banco, tenant_id: UUID) -> dict[str, UUID]:
    grupo, empresa, unidade = uuid4(), uuid4(), uuid4()
    inventario, fonte, calculo, lancamento = uuid4(), uuid4(), uuid4(), uuid4()
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
        await admin.execute(
            "INSERT INTO inventario (tenant_id, inventario_id, empresa_id, ano, abordagem)"
            " VALUES ($1, $2, $3, 2025, 'controle_operacional')",
            tenant_id,
            inventario,
            empresa,
        )
        await admin.execute(
            "INSERT INTO fonte_emissao (tenant_id, fonte_id, empresa_id, inventario_id, unidade_id,"
            " categoria, descricao, unidade_medida, periodicidade)"
            " VALUES ($1, $2, $3, $4, $5, 'escopo1.combustao_movel', 'Frota de entregas', 'L',"
            " 'mensal')",
            tenant_id,
            fonte,
            empresa,
            inventario,
            unidade,
        )
        # Dado lancado por outra pessoa (sem autor): a tela mostra aprovar e devolver.
        await admin.execute(
            "INSERT INTO lancamento (tenant_id, lancamento_id, empresa_id, fonte_id, periodo,"
            " versao, quantidade, unidade, origem) VALUES ($1, $4, $2, $3, 1, 1,"
            " 1234.5, 'L', 'manual')",
            tenant_id,
            empresa,
            fonte,
            lancamento,
        )
        # Calculo ja feito (valores quaisquer: a tela so mostra o que esta gravado).
        await admin.execute(
            "INSERT INTO calculo (tenant_id, calculo_id, empresa_id, inventario_id, metodo,"
            " metodo_versao, gwp_conjunto, conhecido_em, entrada, hash_entrada, hash_resultado,"
            " escopo1_tco2e, biogenico_tco2, linhas, pendencias) VALUES ($1, $2, $3, $4,"
            " 'fator_direto', '1.0.0', 'AR5_100', now(), '{}', $5, $5, 3.2, 0.4, 1, 1)",
            tenant_id,
            calculo,
            empresa,
            inventario,
            bytes(range(32)),
        )
        await admin.execute(
            "INSERT INTO calculo_linha (tenant_id, calculo_id, linha, empresa_id, fonte_id,"
            " lancamento_id, periodo, categoria, atividade, componente, data_referencia,"
            " quantidade, unidade, quantidade_convertida, gas, biogenico, fator, unidade_fator,"
            " fator_versao_id, gwp, gwp_versao_id, percentual_consolidado, massa_t, tco2e)"
            " VALUES ($1, $2, 1, $3, $4, $5, 1, 'escopo1.combustao_movel', 'x', 'x',"
            " '2025-01-31', 1234.5, 'L', 1234.5, 'CO2', false, 2.6, 'kg/L', gen_random_uuid(),"
            " 1, gen_random_uuid(), 100, 3.2, 3.2)",
            tenant_id,
            calculo,
            empresa,
            fonte,
            lancamento,
        )
        await admin.execute(
            "INSERT INTO calculo_pendencia (tenant_id, calculo_id, item, empresa_id, fonte_id,"
            " periodo, motivo, detalhe) VALUES ($1, $2, 1, $3, $4, 2, 'falta_dado', 'sem dado')",
            tenant_id,
            calculo,
            empresa,
            fonte,
        )
        meta = uuid4()
        await admin.execute(
            "INSERT INTO meta (tenant_id, meta_id, empresa_id, nome, escopos, inventario_base_id,"
            " calculo_base_id, ano_base, valor_base_tco2e, ano_alvo, reducao_percentual)"
            " VALUES ($1, $2, $3, 'Meta acessível', '1+2', $4, $5, 2025, 3.2, 2030, 40)",
            tenant_id,
            meta,
            empresa,
            inventario,
            calculo,
        )
        await admin.execute(
            "INSERT INTO acao_reducao (tenant_id, acao_id, empresa_id, meta_id, titulo,"
            " reducao_estimada_tco2e, ano_inicio) VALUES ($1, gen_random_uuid(), $2, $3,"
            " 'Trocar a frota', 1, 2026)",
            tenant_id,
            empresa,
            meta,
        )
    finally:
        await admin.close()
    return {
        "meta": meta,
        "empresa": empresa,
        "unidade": unidade,
        "inventario": inventario,
        "fonte": fonte,
        "calculo": calculo,
    }


@pytest.fixture(scope="module")
def pagina_logada(portal: str, banco: Banco) -> Iterator[tuple[Any, dict[str, UUID]]]:
    tenant_id, token = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Acessivel", "pessoa@acessivel.example")
    )
    ids = asyncio.run(_estrutura(banco, tenant_id))
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
        yield pagina, ids
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
    "/coleta",
    "/coleta/inventarios/{inventario}",
    "/coleta/inventarios/{inventario}/fontes/nova",
    "/coleta/fontes/{fonte}",
    "/coleta/fontes/{fonte}/editar",
    "/coleta/fontes/{fonte}/periodos/1",
    "/coleta/fontes/{fonte}/periodos/2",
    "/resultados",
    "/resultados/calculos/{calculo}",
    "/fechamento",
    "/verificacao",
    "/relatorios",
    "/usuarios",
    "/metas",
    "/metas/{meta}",
    "/integridade",
    "/painel",
    "/meus-dados",
]


@pytest.mark.parametrize("modelo", TELAS)
def test_telas_logadas_sem_violacao(
    portal: str, pagina_logada: tuple[Any, dict[str, UUID]], modelo: str
) -> None:
    pagina, ids = pagina_logada
    erros = vigiar_console(pagina)
    caminho = modelo.format(**ids)
    pagina.goto(portal + caminho, wait_until="networkidle")
    if caminho.endswith("000000000000"):
        assert pagina.get_by_text("Página não encontrada").is_visible()
        erros = [e for e in erros if "status of 404" not in e]
    assert not erros, erros
    violacoes = violacoes_axe(pagina)
    assert not violacoes, violacoes


def test_formulario_mostra_todos_os_erros(
    portal: str, pagina_logada: tuple[Any, dict[str, UUID]]
) -> None:
    pagina, _ids = pagina_logada
    pagina.goto(portal + "/organizacao/empresas/nova", wait_until="networkidle")
    pagina.fill("#campo-cnpj", "11.222.333/0001-82")
    pagina.get_by_role("button", name="Salvar empresa").click()
    pagina.get_by_text("Corrija os").wait_for()
    for campo in ("cnpj", "nome", "municipio"):
        assert pagina.locator(f"#campo-{campo}").get_attribute("aria-invalid") == "true", campo
    assert "dígitos finais" in pagina.locator("#erro-cnpj").text_content()
    assert not violacoes_axe(pagina)


async def _verificador(banco: Banco, ids: dict[str, UUID], tenant_id: UUID) -> None:
    """Pessoa com identidade no provedor simulado e uma concessao aceita para o inventario."""
    admin = await asyncpg.connect(banco.dsn_admin)
    try:
        usuario = uuid4()
        await admin.execute(
            "INSERT INTO usuario (usuario_id, email, nome) VALUES ($1, 'verif@acessivel.example',"
            " 'Verificadora')",
            usuario,
        )
        await admin.execute(
            "INSERT INTO identidade (provedor, sujeito, usuario_id) VALUES ('dev',"
            " 'verificadora-acessivel', $1)",
            usuario,
        )
        criador = await admin.fetchval(
            "SELECT usuario_id FROM membro WHERE tenant_id = $1 LIMIT 1", tenant_id
        )
        await admin.execute(
            "INSERT INTO concessao_verificacao (tenant_id, concessao_id, empresa_id,"
            " inventario_id, email, segredo_hash, criado_por, expira_em, aceito_em, usuario_id)"
            " VALUES ($1, gen_random_uuid(), $2, $3, 'verif@acessivel.example', $4, $5,"
            " now() + interval '30 days', now(), $6)",
            tenant_id,
            ids["empresa"],
            ids["inventario"],
            bytes(32),
            criador,
            usuario,
        )
    finally:
        await admin.close()


def test_telas_do_verificador_sem_violacao(
    portal: str, banco: Banco, pagina_logada: tuple[Any, dict[str, UUID]]
) -> None:
    pagina_do_cliente, ids = pagina_logada

    async def preparar() -> None:
        await _verificador(banco, ids, await _tenant_da_empresa(banco, ids["empresa"]))

    # O Playwright sincrono ja roda um event loop nesta thread: o banco vai noutra.
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, preparar()).result()
    contexto = pagina_do_cliente.context.browser.new_context(locale="pt-BR")
    try:
        pagina = contexto.new_page()
        pagina.goto(f"{portal}/entrar/dev")
        pagina.fill("input[name=username]", "verificadora-acessivel")
        pagina.fill(
            "textarea[name=claims]",
            json.dumps({"email": "verif@acessivel.example", "email_verified": True}),
        )
        pagina.click("input[type=submit]")
        pagina.wait_for_url(f"{portal}/clientes", timeout=60_000)
        assert not violacoes_axe(pagina)
        pagina.get_by_role("button", name="Abrir a verificação").click()
        pagina.wait_for_url(f"{portal}/verificacao/painel", timeout=30_000)
        for caminho in ("/verificacao/painel", f"/verificacao/fontes/{ids['fonte']}"):
            erros = vigiar_console(pagina)
            pagina.goto(portal + caminho, wait_until="networkidle")
            assert not erros, erros
            violacoes = violacoes_axe(pagina)
            assert not violacoes, (caminho, violacoes)
    finally:
        contexto.close()


async def _tenant_da_empresa(banco: Banco, empresa: UUID) -> UUID:
    admin = await asyncpg.connect(banco.dsn_admin)
    try:
        return UUID(
            str(
                await admin.fetchval("SELECT tenant_id FROM empresa WHERE empresa_id = $1", empresa)
            )
        )
    finally:
        await admin.close()
