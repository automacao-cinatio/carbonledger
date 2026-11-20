"""Postgres real com os papeis e as migracoes de verdade, e dados de dois tenants."""

import asyncio
import os
import subprocess  # nosec B404
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
from testcontainers.community.postgres import PostgresContainer

RAIZ = Path(__file__).resolve().parents[2]
PAPEIS_SQL = RAIZ / "infra" / "sql" / "papeis.sql"

# Senhas so deste conteiner descartavel de teste.
SENHA_OWNER = "owner-teste"
SENHA_USER = "user-teste"


@dataclass(frozen=True)
class Banco:
    dsn_admin: str
    dsn_owner: str
    dsn_app: str


@dataclass(frozen=True)
class Dados:
    """Dois tenants (A e B), cada um com duas empresas."""

    tenant_a: UUID
    tenant_b: UUID
    empresa_a1: UUID
    empresa_a2: UUID
    empresa_b1: UUID
    empresa_b2: UUID
    admin_a: UUID
    responsavel_a1: UUID
    admin_b: UUID
    responsavel_b1: UUID


DADOS = Dados(
    tenant_a=UUID("aaaaaaaa-0000-4000-8000-000000000000"),
    tenant_b=UUID("bbbbbbbb-0000-4000-8000-000000000000"),
    empresa_a1=UUID("aaaaaaaa-0000-4000-8000-000000000001"),
    empresa_a2=UUID("aaaaaaaa-0000-4000-8000-000000000002"),
    empresa_b1=UUID("bbbbbbbb-0000-4000-8000-000000000001"),
    empresa_b2=UUID("bbbbbbbb-0000-4000-8000-000000000002"),
    admin_a=UUID("aaaaaaaa-1111-4000-8000-000000000001"),
    responsavel_a1=UUID("aaaaaaaa-1111-4000-8000-000000000002"),
    admin_b=UUID("bbbbbbbb-1111-4000-8000-000000000001"),
    responsavel_b1=UUID("bbbbbbbb-1111-4000-8000-000000000002"),
)


def _dsn(conteiner: PostgresContainer, usuario: str, senha: str) -> str:
    host = conteiner.get_container_host_ip()
    porta = conteiner.get_exposed_port(5432)
    return f"postgresql://{usuario}:{senha}@{host}:{porta}/{conteiner.dbname}"


async def _preparar_papeis(dsn_admin: str) -> None:
    conexao = await asyncpg.connect(dsn_admin)
    try:
        await conexao.execute(PAPEIS_SQL.read_text(encoding="utf-8"))
        for papel, senha in (("app_owner", SENHA_OWNER), ("app_user", SENHA_USER)):
            comando = await conexao.fetchval(
                "SELECT format('ALTER ROLE %I PASSWORD %L', $1::text, $2::text)", papel, senha
            )
            await conexao.execute(comando)
    finally:
        await conexao.close()


async def _semear(dsn_admin: str) -> None:
    """Dados de referencia inseridos pelo superusuario (que ignora RLS)."""
    conexao = await asyncpg.connect(dsn_admin)
    try:
        await conexao.executemany(
            "INSERT INTO tenant (tenant_id, nome) VALUES ($1, $2)",
            [(DADOS.tenant_a, "Tenant A"), (DADOS.tenant_b, "Tenant B")],
        )
        await conexao.executemany(
            "INSERT INTO empresa (tenant_id, empresa_id, nome) VALUES ($1, $2, $3)",
            [
                (DADOS.tenant_a, DADOS.empresa_a1, "Empresa A1"),
                (DADOS.tenant_a, DADOS.empresa_a2, "Empresa A2"),
                (DADOS.tenant_b, DADOS.empresa_b1, "Empresa B1"),
                (DADOS.tenant_b, DADOS.empresa_b2, "Empresa B2"),
            ],
        )
        await conexao.executemany(
            "INSERT INTO usuario (usuario_id, email, nome) VALUES ($1, $2, $3)",
            [
                (DADOS.admin_a, "admin@a.example", "Admin A"),
                (DADOS.responsavel_a1, "resp@a.example", "Responsavel A1"),
                (DADOS.admin_b, "admin@b.example", "Admin B"),
                (DADOS.responsavel_b1, "resp@b.example", "Responsavel B1"),
            ],
        )
        await conexao.executemany(
            "INSERT INTO membro (tenant_id, usuario_id, perfil) VALUES ($1, $2, $3)",
            [
                (DADOS.tenant_a, DADOS.admin_a, "administrador"),
                (DADOS.tenant_a, DADOS.responsavel_a1, "responsavel"),
                (DADOS.tenant_b, DADOS.admin_b, "administrador"),
                (DADOS.tenant_b, DADOS.responsavel_b1, "responsavel"),
            ],
        )
        await conexao.executemany(
            "INSERT INTO membro_empresa (tenant_id, usuario_id, empresa_id) VALUES ($1, $2, $3)",
            [
                (DADOS.tenant_a, DADOS.responsavel_a1, DADOS.empresa_a1),
                (DADOS.tenant_b, DADOS.responsavel_b1, DADOS.empresa_b1),
            ],
        )
        await conexao.executemany(
            "INSERT INTO convite (tenant_id, convite_id, email, perfil, segredo_hash, expira_em)"
            " VALUES ($1, $2, $3, 'gestor', $4, now() + interval '7 days')",
            [
                (DADOS.tenant_a, uuid4(), "gestor@a.example", bytes(32)),
                (DADOS.tenant_b, uuid4(), "gestor@b.example", bytes(32)),
            ],
        )
    finally:
        await conexao.close()


def migrar(dsn_owner: str, *argumentos: str) -> subprocess.CompletedProcess[str]:
    """Roda o Alembic de verdade, em outro processo, como app_owner."""
    ambiente = {**os.environ, "MIGRACAO_DATABASE_URL": dsn_owner}
    return subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-m", "alembic", *argumentos],
        cwd=RAIZ,
        env=ambiente,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="session")
def banco() -> Iterator[Banco]:
    with PostgresContainer("postgres:16", driver=None) as conteiner:
        dsn_admin = _dsn(conteiner, conteiner.username, conteiner.password)
        asyncio.run(_preparar_papeis(dsn_admin))
        dsn_owner = _dsn(conteiner, "app_owner", SENHA_OWNER)
        resultado = migrar(dsn_owner, "upgrade", "head")
        assert resultado.returncode == 0, resultado.stderr
        asyncio.run(_semear(dsn_admin))
        yield Banco(
            dsn_admin=dsn_admin,
            dsn_owner=dsn_owner,
            dsn_app=_dsn(conteiner, "app_user", SENHA_USER),
        )


@pytest.fixture
def dados() -> Dados:
    return DADOS


@pytest.fixture
async def conexao_app(banco: Banco) -> AsyncIterator[asyncpg.Connection]:
    conexao = await asyncpg.connect(banco.dsn_app)
    try:
        yield conexao
    finally:
        await conexao.close()


@pytest.fixture
async def conexao_admin(banco: Banco) -> AsyncIterator[asyncpg.Connection]:
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        yield conexao
    finally:
        await conexao.close()
