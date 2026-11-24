"""Contexto de tenant nas tarefas Celery, com asyncpg e o RLS do banco."""

import asyncio
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.core.config import obter_configuracao
from app.core.tenancy import ContextoTenant
from app.workers.registro import TarefaSemTenant
from app.workers.tarefas import tarefa_de_tenant
from tests.isolation.conftest import Banco, Dados

pytestmark = pytest.mark.integration


class FalhaProposital(RuntimeError):
    pass


@tarefa_de_tenant("teste_isolamento.listar_empresas")
async def listar_empresas(conexao: Any, contexto: ContextoTenant) -> dict[str, Any]:
    linhas = await conexao.fetch("SELECT empresa_id FROM empresa ORDER BY empresa_id")
    return {
        "empresas": [str(linha["empresa_id"]) for linha in linhas],
        "tenant_do_contexto": str(contexto.tenant_id),
    }


@tarefa_de_tenant("teste_isolamento.criar_empresa")
async def criar_empresa(
    conexao: Any, contexto: ContextoTenant, *, empresa_id: str, falhar: bool = False
) -> None:
    await conexao.execute(
        "INSERT INTO empresa (tenant_id, empresa_id, grupo_id, nome)"
        " VALUES ($1, $2, (SELECT grupo_id FROM grupo LIMIT 1), 'Tarefa')",
        contexto.tenant_id,
        UUID(empresa_id),
    )
    if falhar:
        raise FalhaProposital


@pytest.fixture
def banco_da_tarefa(banco: Banco, monkeypatch: pytest.MonkeyPatch) -> Iterator[Banco]:
    monkeypatch.setenv("DATABASE_URL", banco.dsn_app)
    obter_configuracao.cache_clear()
    yield banco
    obter_configuracao.cache_clear()


async def _contar(banco: Banco, empresa_id: UUID) -> int:
    admin = await asyncpg.connect(banco.dsn_admin)
    try:
        return int(
            await admin.fetchval("SELECT count(*) FROM empresa WHERE empresa_id = $1", empresa_id)
        )
    finally:
        await admin.close()


async def _apagar(banco: Banco, empresa_id: UUID) -> None:
    admin = await asyncpg.connect(banco.dsn_admin)
    try:
        await admin.execute("DELETE FROM empresa WHERE empresa_id = $1", empresa_id)
    finally:
        await admin.close()


# As tarefas chamam asyncio.run(); por isso estes testes sao sincronos.


def test_tarefa_ve_so_o_proprio_tenant(banco_da_tarefa: Banco, dados: Dados) -> None:
    resultado = listar_empresas(tenant_id=str(dados.tenant_b))
    assert resultado == {
        "empresas": sorted([str(dados.empresa_b1), str(dados.empresa_b2)]),
        "tenant_do_contexto": str(dados.tenant_b),
    }


def test_tarefa_com_escopo_de_empresas(banco_da_tarefa: Banco, dados: Dados) -> None:
    resultado = listar_empresas(
        tenant_id=str(dados.tenant_a), empresas=[str(dados.empresa_a1), str(dados.empresa_b1)]
    )
    assert resultado["empresas"] == [str(dados.empresa_a1)]


def test_tarefa_pelo_caminho_do_celery(banco_da_tarefa: Banco, dados: Dados) -> None:
    """apply() roda a tarefa localmente pelo mesmo caminho do worker."""
    resultado = listar_empresas.apply(kwargs={"tenant_id": str(dados.tenant_a)}).get()
    assert resultado["empresas"] == sorted([str(dados.empresa_a1), str(dados.empresa_a2)])


def test_tarefa_sem_tenant_nao_roda(banco_da_tarefa: Banco) -> None:
    with pytest.raises(TarefaSemTenant):
        listar_empresas()


async def test_tarefa_grava_com_commit(banco_da_tarefa: Banco, dados: Dados) -> None:
    nova = uuid4()
    try:
        await _rodar_em_thread(criar_empresa, tenant_id=str(dados.tenant_a), empresa_id=str(nova))
        assert await _contar(banco_da_tarefa, nova) == 1
    finally:
        await _apagar(banco_da_tarefa, nova)


async def test_erro_na_tarefa_desfaz_a_gravacao(banco_da_tarefa: Banco, dados: Dados) -> None:
    nova = uuid4()
    with pytest.raises(FalhaProposital):
        await _rodar_em_thread(
            criar_empresa, tenant_id=str(dados.tenant_a), empresa_id=str(nova), falhar=True
        )
    assert await _contar(banco_da_tarefa, nova) == 0


async def test_tarefa_nao_grava_fora_do_escopo(banco_da_tarefa: Banco, dados: Dados) -> None:
    """Escopo so com a empresa B1: criar outra empresa e recusado pela politica de empresa."""
    nova = uuid4()
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await _rodar_em_thread(
            criar_empresa,
            tenant_id=str(dados.tenant_b),
            empresas=[str(dados.empresa_b1)],
            empresa_id=str(nova),
        )
    assert await _contar(banco_da_tarefa, nova) == 0


async def _rodar_em_thread(tarefa: Any, **kwargs: Any) -> Any:
    """A tarefa usa asyncio.run(); dentro de um teste async ela precisa de outra thread."""
    return await asyncio.to_thread(tarefa, **kwargs)
