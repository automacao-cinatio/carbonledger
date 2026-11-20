"""Regra das tarefas: toda tarefa recebe tenant_id explicito, salvo as globais listadas aqui.

Tarefa global (manutencao, disparo por tenant feito pelo beat) entra em TAREFAS_GLOBAIS
com a justificativa. Ela nao toca dado de tenant: no maximo enfileira tarefas de tenant.
"""

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

# Nome da tarefa -> justificativa.
TAREFAS_GLOBAIS: dict[str, str] = {}

# Preenchido por tarefa_de_tenant(); serve ao teste que confere o app inteiro.
TAREFAS_DE_TENANT: set[str] = set()

PREFIXO_INTERNO_CELERY = "celery."


class TarefaSemTenant(ValueError):
    """Tarefa publicada ou executada sem tenant_id valido e nomeado."""


def eh_global(nome: str) -> bool:
    return nome in TAREFAS_GLOBAIS or nome.startswith(PREFIXO_INTERNO_CELERY)


def exigir_tenant(nome: str, args: Sequence[Any] | None, kwargs: Mapping[str, Any] | None) -> None:
    """Recusa a publicacao de tarefa de tenant sem tenant_id nomeado e valido."""
    if eh_global(nome):
        return
    if args:
        raise TarefaSemTenant(f"{nome}: tarefa de tenant aceita so argumentos nomeados")
    tenant_id = (kwargs or {}).get("tenant_id")
    if tenant_id is None:
        raise TarefaSemTenant(f"{nome}: tenant_id e obrigatorio")
    try:
        UUID(str(tenant_id))
    except ValueError:
        raise TarefaSemTenant(f"{nome}: tenant_id invalido") from None
