"""Contexto de tenant aplicado por transacao.

O contexto vive em configuracoes locais da transacao (set_config com is_local = true):
app.tenant_id, app.escopo, app.empresas e app.usuario_id. Ao fim da transacao ele some,
e uma conexao reaproveitada do pool volta a nao enxergar nada. Nunca use SET de sessao.

Escopos:
- tenant: todas as empresas do tenant.
- empresas: so as empresas listadas (lista vazia = nenhuma).
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID


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
