from app.core.db import dsn_asyncpg, exige_tls, url_sqlalchemy_asyncpg


def test_remove_prefixo_de_driver() -> None:
    assert dsn_asyncpg("postgresql+asyncpg://u:p@h:5432/d") == "postgresql://u:p@h:5432/d"


def test_remove_ssl_disable() -> None:
    assert dsn_asyncpg("postgresql://u:p@h/d?ssl=disable") == "postgresql://u:p@h/d"


def test_mantem_outros_parametros() -> None:
    assert (
        dsn_asyncpg("postgresql://u:p@h/d?ssl=disable&application_name=x")
        == "postgresql://u:p@h/d?application_name=x"
    )


def test_sslmode_vira_ssl_so_para_o_sqlalchemy() -> None:
    url = "postgresql://u:p@banco:5432/cl?sslmode=require"
    assert dsn_asyncpg(url) == url  # o asyncpg direto entende sslmode
    assert url_sqlalchemy_asyncpg(url) == "postgresql+asyncpg://u:p@banco:5432/cl?ssl=require"


def test_exige_tls() -> None:
    assert exige_tls("postgresql://u:p@banco/cl?sslmode=require")
    assert exige_tls("postgresql://u:p@banco/cl?sslmode=verify-full")
    assert not exige_tls("postgresql://u:p@banco/cl")
    assert not exige_tls("postgresql://u:p@banco/cl?sslmode=prefer")
    assert not exige_tls("postgresql://u:p@banco/cl?ssl=disable")
