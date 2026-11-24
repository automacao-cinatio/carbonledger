import pytest

from app.core.db import url_sqlalchemy_asyncpg
from app.core.rls import sql_conceder, sql_rls_tabela


def test_rls_de_tabela_por_tenant() -> None:
    comandos = sql_rls_tabela("tenant", por_empresa=False)
    assert comandos[0] == "ALTER TABLE tenant ENABLE ROW LEVEL SECURITY"
    assert comandos[1] == "ALTER TABLE tenant FORCE ROW LEVEL SECURITY"
    assert len(comandos) == 3
    assert "AS PERMISSIVE FOR ALL" in comandos[2]
    assert comandos[2].count("contexto_tenant_id()") == 2


def test_rls_de_tabela_por_empresa_tem_politica_restritiva() -> None:
    comandos = sql_rls_tabela("empresa", por_empresa=True)
    assert len(comandos) == 4
    assert "AS RESTRICTIVE FOR ALL" in comandos[3]
    assert comandos[3].count("contexto_empresas()") == 2


@pytest.mark.parametrize("nome", ["Empresa", "empresa; DROP TABLE x", "1tabela", "", "a" * 64])
def test_identificador_invalido_e_recusado(nome: str) -> None:
    with pytest.raises(ValueError, match="identificador"):
        sql_rls_tabela(nome, por_empresa=False)
    with pytest.raises(ValueError, match="identificador"):
        sql_conceder(nome, ["SELECT"])


def test_conceder_da_ao_verificador_so_select() -> None:
    assert sql_conceder("empresa", ["select", "insert"]) == [
        "REVOKE ALL ON empresa FROM PUBLIC",
        "GRANT SELECT, INSERT ON empresa TO app_user",
        "GRANT SELECT ON empresa TO app_verificador",
    ]


@pytest.mark.parametrize("privilegios", [[], ["TRUNCATE"], ["SELECT", "ALL"]])
def test_privilegio_invalido_e_recusado(privilegios: list[str]) -> None:
    with pytest.raises(ValueError, match="privilegios"):
        sql_conceder("empresa", privilegios)


def test_url_sqlalchemy_asyncpg() -> None:
    assert (
        url_sqlalchemy_asyncpg("postgresql://u:p@h:5432/d?ssl=disable")
        == "postgresql+asyncpg://u:p@h:5432/d"
    )
    assert url_sqlalchemy_asyncpg("postgresql+psycopg://u:p@h/d") == "postgresql+asyncpg://u:p@h/d"
