"""Integridade no banco: a semente tem calculo sem entrada e arquivos com hash ficticio, entao
a verificacao precisa acusar; o resultado fica gravado (so insercao) e vai para a trilha."""

from uuid import uuid4

import asyncpg
import pytest

from app.core.tenancy import ContextoTenant, Escopo, aplicar_contexto
from app.modules.integridade import servico
from tests.isolation.conftest import Dados
from tests.isolation.portal import ArmazemMemoria

pytestmark = pytest.mark.integration


async def test_verificacao_acusa_o_que_nao_confere(
    conexao_app: asyncpg.Connection, dados: Dados
) -> None:
    transacao = conexao_app.transaction()
    await transacao.start()
    try:
        await aplicar_contexto(
            conexao_app, ContextoTenant(tenant_id=dados.tenant_a, escopo=Escopo.TENANT)
        )
        armazem = ArmazemMemoria()
        armazem.objetos["x"] = (b"outro conteudo", "application/pdf")
        relatorio = await servico.verificar(
            conexao_app, dados.tenant_a, armazem, origem="manual", pedida_por=dados.admin_a
        )
        assert not relatorio.integra
        tipos = {f.tipo for f in relatorio.falhas}
        # Calculo da semente sem entrada valida; evidencia e planilha com SHA-256 ficticio.
        assert tipos == {"calculo", "evidencias", "planilhas"}
        assert relatorio.contagens["eventos_da_trilha"] > 0
        motivos = {f.motivo for f in relatorio.falhas}
        assert "SHA-256 diferente do registrado" in motivos
        gravado = await conexao_app.fetchrow(
            "SELECT integra, jsonb_array_length(falhas) AS falhas, origem"
            " FROM integridade_verificacao WHERE verificacao_id = $1",
            relatorio.verificacao_id,
        )
        assert gravado["integra"] is False and gravado["falhas"] == len(relatorio.falhas)
        assert gravado["origem"] == "manual"
        trilha = await conexao_app.fetchval(
            "SELECT count(*) FROM audit_log WHERE acao = 'integridade.verificada'"
            " AND entidade_id = $1",
            str(relatorio.verificacao_id),
        )
        assert trilha == 1
    finally:
        await transacao.rollback()


async def test_objeto_sumido_tambem_e_falha(conexao_app: asyncpg.Connection, dados: Dados) -> None:
    transacao = conexao_app.transaction()
    await transacao.start()
    try:
        await aplicar_contexto(
            conexao_app, ContextoTenant(tenant_id=dados.tenant_b, escopo=Escopo.TENANT)
        )
        relatorio = await servico.verificar(
            conexao_app, dados.tenant_b, ArmazemMemoria(), origem="agendada"
        )
        inacessiveis = [f for f in relatorio.falhas if "inacessível" in f.motivo]
        assert {f.tipo for f in inacessiveis} == {"evidencias", "planilhas"}
    finally:
        await transacao.rollback()


async def test_resultado_inconsistente_e_recusado(
    conexao_app: asyncpg.Connection, dados: Dados
) -> None:
    """integra precisa bater com a lista de falhas: nao da para gravar 'integra' com falha."""
    async with conexao_app.transaction():
        await aplicar_contexto(
            conexao_app, ContextoTenant(tenant_id=dados.tenant_a, escopo=Escopo.TENANT)
        )
        with pytest.raises(asyncpg.CheckViolationError):
            async with conexao_app.transaction():
                await conexao_app.execute(
                    "INSERT INTO integridade_verificacao (tenant_id, verificacao_id, origem,"
                    " iniciada_em, contagens, falhas, integra) VALUES ($1, $2, 'manual', now(),"
                    " '{}', '[{\"tipo\": \"trilha\"}]', true)",
                    dados.tenant_a,
                    uuid4(),
                )


async def test_disparo_so_ve_ids_dos_tenants(conexao_app: asyncpg.Connection, dados: Dados) -> None:
    ids = {linha[0] for linha in await conexao_app.fetch("SELECT identidade_tenants()")}
    assert {dados.tenant_a, dados.tenant_b} <= ids
    # Sem contexto, a tabela de tenants continua invisivel ao app_user.
    assert await conexao_app.fetchval("SELECT count(*) FROM tenant") == 0
