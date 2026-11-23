"""Cobertura de RLS: o catalogo do banco confere com as regras do CLAUDE.md.

Falha quando uma tabela nova com tenant_id fica sem RLS, sem FORCE, sem politica,
com dono errado ou fora do registro da suite de vazamento.
"""

import asyncpg
import pytest

from tests.isolation.conftest import Banco, Dados, migrar
from tests.isolation.tabelas import TABELAS_COM_TENANT, TABELAS_GLOBAIS

pytestmark = pytest.mark.integration


async def _tabelas(conexao: asyncpg.Connection) -> dict[str, set[str]]:
    linhas = await conexao.fetch(
        """
        SELECT c.relname AS tabela, a.attname AS coluna
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
        """
    )
    tabelas: dict[str, set[str]] = {}
    for linha in linhas:
        tabelas.setdefault(linha["tabela"], set()).add(linha["coluna"])
    return tabelas


async def test_registro_confere_com_o_banco(conexao_admin: asyncpg.Connection) -> None:
    tabelas = await _tabelas(conexao_admin)
    com_tenant = {t for t, colunas in tabelas.items() if "tenant_id" in colunas}
    sem_tenant = set(tabelas) - com_tenant

    assert com_tenant == set(TABELAS_COM_TENANT), (
        "Toda tabela com tenant_id entra em tests/isolation/tabelas.py no mesmo PR"
    )
    assert sem_tenant <= set(TABELAS_GLOBAIS), (
        f"Tabelas sem tenant_id e sem justificativa: {sorted(sem_tenant - set(TABELAS_GLOBAIS))}"
    )
    for tabela, por_empresa in TABELAS_COM_TENANT.items():
        assert ("empresa_id" in tabelas[tabela]) is por_empresa, tabela


@pytest.mark.parametrize("tabela", sorted(TABELAS_COM_TENANT))
async def test_rls_ligado_com_force_e_dono_certo(
    conexao_admin: asyncpg.Connection, tabela: str
) -> None:
    linha = await conexao_admin.fetchrow(
        """
        SELECT c.relrowsecurity, c.relforcerowsecurity, pg_get_userbyid(c.relowner) AS dono
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = $1
        """,
        tabela,
    )
    assert linha is not None
    assert linha["relrowsecurity"], f"{tabela} sem ENABLE ROW LEVEL SECURITY"
    assert linha["relforcerowsecurity"], f"{tabela} sem FORCE ROW LEVEL SECURITY"
    assert linha["dono"] == "app_owner"


@pytest.mark.parametrize("tabela", sorted(TABELAS_COM_TENANT))
async def test_politicas_de_tenant_e_de_empresa(
    conexao_admin: asyncpg.Connection, tabela: str
) -> None:
    politicas = await conexao_admin.fetch(
        "SELECT permissive, cmd, roles, qual, with_check FROM pg_policies"
        " WHERE schemaname = 'public' AND tablename = $1",
        tabela,
    )
    permissivas = [p for p in politicas if p["permissive"] == "PERMISSIVE"]
    restritivas = [p for p in politicas if p["permissive"] == "RESTRICTIVE"]

    # Uma unica permissiva, de tenant: outra permissiva abriria o acesso por OR.
    assert len(permissivas) == 1, f"{tabela}: esperada uma politica permissiva de tenant"
    tenant = permissivas[0]
    assert tenant["cmd"] == "ALL"
    assert list(tenant["roles"]) == ["public"]
    assert "contexto_tenant_id()" in tenant["qual"]
    assert "contexto_tenant_id()" in tenant["with_check"]

    empresa = [p for p in restritivas if "contexto_empresas()" in (p["qual"] or "")]
    if TABELAS_COM_TENANT[tabela]:
        assert len(empresa) == 1, f"{tabela}: falta a politica restritiva de empresa"
        assert empresa[0]["cmd"] == "ALL"
        assert "contexto_empresas()" in empresa[0]["with_check"]
    else:
        assert not empresa


@pytest.mark.parametrize("tabela", sorted(TABELAS_COM_TENANT))
async def test_tenant_id_obrigatorio_e_indices_comecam_por_ele(
    conexao_admin: asyncpg.Connection, tabela: str
) -> None:
    nulo = await conexao_admin.fetchval(
        "SELECT NOT attnotnull FROM pg_attribute"
        " WHERE attrelid = $1::regclass AND attname = 'tenant_id'",
        tabela,
    )
    assert nulo is False, f"{tabela}.tenant_id aceita NULL"

    indices = await conexao_admin.fetch(
        """
        SELECT i.indexrelid::regclass::text AS indice, a.attname AS primeira
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
        WHERE i.indrelid = $1::regclass
        """,
        tabela,
    )
    assert indices, f"{tabela} sem indice"
    for indice in indices:
        assert indice["primeira"] == "tenant_id", f"{indice['indice']} nao comeca por tenant_id"


@pytest.mark.parametrize("tabela", sorted(TABELAS_COM_TENANT))
async def test_semente_cobre_os_dois_tenants(
    conexao_admin: asyncpg.Connection, dados: Dados, tabela: str
) -> None:
    """Sem linhas dos dois tenants, a suite de vazamento passaria sem provar nada."""
    tenants = await conexao_admin.fetchval(
        f"SELECT array_agg(DISTINCT tenant_id) FROM {tabela}"  # noqa: S608  # nosec B608
    )
    assert {dados.tenant_a, dados.tenant_b} <= set(tenants or [])


async def test_papeis_sem_privilegio_de_contornar_rls(conexao_admin: asyncpg.Connection) -> None:
    papeis = {
        linha["rolname"]: linha
        for linha in await conexao_admin.fetch(
            "SELECT rolname, rolsuper, rolbypassrls, rolcanlogin, rolcreaterole FROM pg_roles"
            " WHERE rolname IN ('app_owner', 'app_user', 'app_verificador')"
        )
    }
    assert set(papeis) == {"app_owner", "app_user", "app_verificador"}
    for nome, papel in papeis.items():
        assert not papel["rolsuper"], nome
        assert not papel["rolbypassrls"], nome
        assert not papel["rolcreaterole"], nome
    assert not papeis["app_verificador"]["rolcanlogin"]

    # app_user nao herda nada de ninguem, e so pode assumir o app_verificador.
    membros = await conexao_admin.fetch(
        "SELECT pg_get_userbyid(roleid) AS papel, inherit_option, set_option"
        " FROM pg_auth_members WHERE member = 'app_user'::regrole"
    )
    assert [(m["papel"], m["inherit_option"], m["set_option"]) for m in membros] == [
        ("app_verificador", False, True)
    ]


@pytest.mark.parametrize("tabela", sorted(TABELAS_COM_TENANT))
async def test_verificador_tem_politica_restritiva_propria(
    conexao_admin: asyncpg.Connection, tabela: str
) -> None:
    """Toda tabela de tenant limita o app_verificador a concessao (ou a nada)."""
    politicas = await conexao_admin.fetch(
        "SELECT permissive, cmd, roles FROM pg_policies"
        " WHERE schemaname = 'public' AND tablename = $1 AND policyname = $2",
        tabela,
        f"{tabela}_verificador",
    )
    assert len(politicas) == 1, f"{tabela}: falta a politica restritiva do verificador"
    assert politicas[0]["permissive"] == "RESTRICTIVE"
    assert list(politicas[0]["roles"]) == ["app_verificador"]


@pytest.mark.parametrize("tabela", sorted(TABELAS_COM_TENANT))
async def test_verificador_so_le(conexao_admin: asyncpg.Connection, tabela: str) -> None:
    for privilegio, esperado in (
        ("SELECT", True),
        ("INSERT", False),
        ("UPDATE", False),
        ("DELETE", False),
        ("TRUNCATE", False),
    ):
        tem = await conexao_admin.fetchval(
            "SELECT has_table_privilege('app_verificador', $1::regclass, $2)", tabela, privilegio
        )
        assert tem is esperado, f"app_verificador {privilegio} em {tabela}"


# Tenant e empresa nunca somem (nada some); tabelas futuras decidem os proprios privilegios.
@pytest.mark.parametrize("tabela", ["tenant", "empresa"])
async def test_app_user_sem_delete_nem_truncate(
    conexao_admin: asyncpg.Connection, tabela: str
) -> None:
    for privilegio in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        tem = await conexao_admin.fetchval(
            "SELECT has_table_privilege('app_user', $1::regclass, $2)", tabela, privilegio
        )
        assert tem is False, f"app_user {privilegio} em {tabela}"


def test_migracao_recusa_papel_que_nao_e_app_owner(banco: Banco) -> None:
    resultado = migrar(banco.dsn_app, "upgrade", "head")
    assert resultado.returncode != 0
    assert "Migracoes rodam como app_owner" in resultado.stderr
