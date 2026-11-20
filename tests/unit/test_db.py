from app.core.db import dsn_asyncpg


def test_remove_prefixo_de_driver() -> None:
    assert dsn_asyncpg("postgresql+asyncpg://u:p@h:5432/d") == "postgresql://u:p@h:5432/d"


def test_remove_ssl_disable() -> None:
    assert dsn_asyncpg("postgresql://u:p@h/d?ssl=disable") == "postgresql://u:p@h/d"


def test_mantem_outros_parametros() -> None:
    assert (
        dsn_asyncpg("postgresql://u:p@h/d?ssl=disable&application_name=x")
        == "postgresql://u:p@h/d?application_name=x"
    )
