"""As migracoes sobem, descem e sobem de novo num banco limpo."""

from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from tests.isolation.conftest import PAPEIS_SQL, Banco, migrar

pytestmark = pytest.mark.integration

BANCO_LIMPO = "migracoes_ida_e_volta"


def _trocar_banco(dsn: str, nome: str) -> str:
    partes = urlsplit(dsn)
    return urlunsplit((partes.scheme, partes.netloc, f"/{nome}", partes.query, partes.fragment))


async def test_upgrade_downgrade_upgrade(banco: Banco) -> None:
    admin = await asyncpg.connect(banco.dsn_admin)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {BANCO_LIMPO}")
        await admin.execute(f"CREATE DATABASE {BANCO_LIMPO}")
    finally:
        await admin.close()

    admin_limpo = await asyncpg.connect(_trocar_banco(banco.dsn_admin, BANCO_LIMPO))
    try:
        await admin_limpo.execute(PAPEIS_SQL.read_text(encoding="utf-8"))
    finally:
        await admin_limpo.close()

    dsn_owner = _trocar_banco(banco.dsn_owner, BANCO_LIMPO)
    for argumentos in (("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head")):
        resultado = migrar(dsn_owner, *argumentos)
        assert resultado.returncode == 0, f"{argumentos}: {resultado.stderr}"

    owner = await asyncpg.connect(dsn_owner)
    try:
        assert await owner.fetchval("SELECT version_num FROM alembic_version") is not None
    finally:
        await owner.close()
