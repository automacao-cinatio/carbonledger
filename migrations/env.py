"""Ambiente do Alembic: migracoes rodam como app_owner, com asyncpg."""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import app.modules.organizacao.modelos  # noqa: F401  registra as tabelas no metadata
from app.core.db import Base, url_sqlalchemy_asyncpg

PAPEL_MIGRACAO = "app_owner"

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    url = os.environ.get("MIGRACAO_DATABASE_URL")
    if not url:
        raise RuntimeError("Defina MIGRACAO_DATABASE_URL (papel app_owner) para migrar.")
    return url_sqlalchemy_asyncpg(url)


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _executar(conexao: Connection) -> None:
    # Tabela criada por outro papel ficaria com outro dono e escaparia do modelo de papeis.
    papel = conexao.execute(text("SELECT current_user")).scalar_one()
    if papel != PAPEL_MIGRACAO:
        raise RuntimeError(f"Migracoes rodam como {PAPEL_MIGRACAO}, nao como {papel}.")
    # A consulta acima abriu uma transacao implicita; sem fecha-la, o Alembic trabalharia
    # dentro dela e nada seria gravado ao fechar a conexao.
    conexao.commit()
    context.configure(
        connection=conexao,
        target_metadata=target_metadata,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _executar_async() -> None:
    motor = create_async_engine(_url(), poolclass=pool.NullPool)
    async with motor.connect() as conexao:
        await conexao.run_sync(_executar)
    await motor.dispose()


def run_migrations_online() -> None:
    asyncio.run(_executar_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
