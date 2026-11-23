"""Painel consolidado: empresa com inventario fechado, empresa sem inventario, filtro por grupo
e a regra de nunca mostrar zero onde nao ha calculo."""

import asyncio
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.cli import criar_tenant
from app.core.config import obter_configuracao
from tests.isolation.conftest import Banco
from tests.isolation.portal import entrar
from tests.isolation.test_verificacao_portal import _dono, _inventario_fechado

pytestmark = pytest.mark.integration


async def _outro_grupo_com_empresa(banco: Banco, tenant_id: UUID) -> UUID:
    grupo = uuid4()
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        await conexao.execute(
            "INSERT INTO grupo (tenant_id, grupo_id, nome) VALUES ($1, $2, 'Grupo Sem Dados')",
            tenant_id,
            grupo,
        )
        await conexao.execute(
            "INSERT INTO empresa (tenant_id, empresa_id, grupo_id, nome)"
            " VALUES ($1, gen_random_uuid(), $2, 'Empresa Nova Ltda')",
            tenant_id,
            grupo,
        )
    finally:
        await conexao.close()
    return grupo


def test_painel_consolidado(app_cliente: TestClient, banco: Banco) -> None:
    cliente = app_cliente
    tenant_id, token = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Painel", "dona@painel.example")
    )
    entrar(cliente, f"/convite/{token}/dev", sujeito="dona-painel", email="dona@painel.example")
    asyncio.run(_inventario_fechado(banco, tenant_id, asyncio.run(_dono(banco, tenant_id))))
    grupo = asyncio.run(_outro_grupo_com_empresa(banco, tenant_id))

    pagina = cliente.get("/painel")
    assert pagina.status_code == 200 and 'aria-current="page"' in pagina.text
    texto = pagina.text
    assert "Empresa Verificada" in texto and "Fechado" in texto and "1,500" in texto
    assert "Empresa Nova Ltda" in texto and "Sem inventário" in texto
    assert "Emissões por empresa" in texto and "Evolução por ano" in texto
    # O inventario 2024 da semente esta em coleta e sem calculo: nao entra na serie anual.
    assert "2024" not in texto.split("Evolução por ano", 1)[1].split("Próximo passo", 1)[0]
    assert "1 empresa(s) ainda sem inventário fechado" in texto

    # So o conteudo da pagina: o seletor de empresa do topo lista todas.
    filtrado = cliente.get(f"/painel?grupo={grupo}").text.split('id="conteudo"', 1)[1]
    assert "Empresa Nova Ltda" in filtrado and "Empresa Verificada" not in filtrado
    assert "Sem cálculos ainda" in filtrado
    # Grupo que nao existe (ou de outro cliente) cai em "todos os grupos".
    assert "Empresa Verificada" in cliente.get(f"/painel?grupo={uuid4()}").text
