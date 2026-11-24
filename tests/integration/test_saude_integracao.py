import pytest
from testcontainers.community.postgres import PostgresContainer

from app.modules.saude.servico import checar_banco

pytestmark = pytest.mark.integration


async def test_checar_banco_com_postgres_real() -> None:
    with PostgresContainer("postgres:16", driver=None) as postgres:
        await checar_banco(postgres.get_connection_url())
