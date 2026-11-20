"""Contexto de tenant aplicado por transacao.

O contexto vive em configuracoes locais da transacao (set_config com is_local = true):
app.tenant_id, app.escopo, app.empresas e app.usuario_id. Ao fim da transacao ele some,
e uma conexao reaproveitada do pool volta a nao enxergar nada. Nunca use SET de sessao.

Escopos:
- tenant: todas as empresas do tenant.
- empresas: so as empresas listadas (lista vazia = nenhuma).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

import asyncpg
from sqlalchemy import Connection, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, SessionTransaction

from app.core.db import dsn_asyncpg


class Escopo(StrEnum):
    TENANT = "tenant"
    EMPRESAS = "empresas"


class ContextoInvalido(ValueError):
    """Contexto de tenant mal formado ou aplicado fora de transacao."""


@dataclass(frozen=True)
class ContextoTenant:
    tenant_id: UUID
    escopo: Escopo
    empresas: frozenset[UUID] = field(default_factory=frozenset)
    usuario_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, UUID):
            raise ContextoInvalido("tenant_id precisa ser UUID")
        if not isinstance(self.escopo, Escopo):
            raise ContextoInvalido("escopo precisa ser tenant ou empresas")
        if any(not isinstance(e, UUID) for e in self.empresas):
            raise ContextoInvalido("empresas precisa conter so UUIDs")
        if self.escopo is Escopo.TENANT and self.empresas:
            raise ContextoInvalido("escopo tenant nao leva lista de empresas")

    def como_configuracoes(self) -> dict[str, str]:
        """Valores de texto para set_config; a ordem das empresas e estavel."""
        empresas = ",".join(str(e) for e in sorted(self.empresas))
        return {
            "app.tenant_id": str(self.tenant_id),
            "app.escopo": self.escopo.value,
            "app.empresas": "{" + empresas + "}",
            "app.usuario_id": "" if self.usuario_id is None else str(self.usuario_id),
        }


SQL_APLICAR_CONTEXTO = (
    "SELECT set_config('app.tenant_id', $1, true),"
    " set_config('app.escopo', $2, true),"
    " set_config('app.empresas', $3, true),"
    " set_config('app.usuario_id', $4, true)"
)


async def aplicar_contexto(conexao: Any, contexto: ContextoTenant) -> None:
    """Aplica o contexto na transacao corrente de uma conexao asyncpg.

    Fora de transacao o set_config local valeria so para um comando e o seguinte rodaria
    sem contexto; por isso recusamos em vez de deixar passar.
    """
    if not conexao.is_in_transaction():
        raise ContextoInvalido("aplicar_contexto exige transacao aberta")
    valores = contexto.como_configuracoes()
    await conexao.execute(
        SQL_APLICAR_CONTEXTO,
        valores["app.tenant_id"],
        valores["app.escopo"],
        valores["app.empresas"],
        valores["app.usuario_id"],
    )


# ---------------------------------------------------------------- SQLAlchemy (API)

CHAVE_CONTEXTO = "contexto_tenant"
# Onde a autenticacao (P04) deixa o contexto resolvido: request.state.<este nome>.
ATRIBUTO_REQUISICAO = "contexto_tenant"

_SQL_CONTEXTO_SA = text(
    "SELECT set_config('app.tenant_id', :tenant_id, true),"
    " set_config('app.escopo', :escopo, true),"
    " set_config('app.empresas', :empresas, true),"
    " set_config('app.usuario_id', :usuario_id, true)"
)


class SessaoTenant(Session):
    """Sessao que aplica o contexto guardado em info a cada transacao que abre.

    Assim um commit no meio da requisicao nao deixa a transacao seguinte sem contexto.
    Sessao sem contexto em info nao aplica nada, e o RLS nao deixa ver nem gravar.
    """


@event.listens_for(SessaoTenant, "after_begin")
def _aplicar_contexto_ao_iniciar(
    sessao: Session, _transacao: SessionTransaction, conexao: Connection
) -> None:
    contexto = sessao.info.get(CHAVE_CONTEXTO)
    if contexto is None:
        return
    if not isinstance(contexto, ContextoTenant):
        raise ContextoInvalido("info da sessao com contexto de tipo errado")
    valores = contexto.como_configuracoes()
    conexao.execute(
        _SQL_CONTEXTO_SA,
        {
            "tenant_id": valores["app.tenant_id"],
            "escopo": valores["app.escopo"],
            "empresas": valores["app.empresas"],
            "usuario_id": valores["app.usuario_id"],
        },
    )


@asynccontextmanager
async def sessao_de_tenant(
    fabrica: async_sessionmaker[AsyncSession], contexto: ContextoTenant
) -> AsyncIterator[AsyncSession]:
    """Sessao com o contexto aplicado; commit ao sair sem erro, rollback se houver erro.

    Sem sessao.begin() em volta: assim a rota pode dar commit no meio, e a transacao
    seguinte (autobegin) recebe o contexto de novo pelo evento after_begin.
    """
    async with fabrica(info={CHAVE_CONTEXTO: contexto}) as sessao:
        try:
            yield sessao
        except BaseException:
            await sessao.rollback()
            raise
        await sessao.commit()


# ---------------------------------------------------------------- asyncpg (workers)


@asynccontextmanager
async def conexao_de_tenant(
    database_url: str, contexto: ContextoTenant
) -> AsyncIterator[asyncpg.Connection]:
    """Conexao asyncpg com uma transacao ja aberta e o contexto aplicado.

    Commit ao sair sem erro; rollback se houver erro. A conexao e fechada no fim.
    """
    conexao = await asyncpg.connect(dsn_asyncpg(database_url))
    try:
        async with conexao.transaction():
            await aplicar_contexto(conexao, contexto)
            yield conexao
    finally:
        await conexao.close()
