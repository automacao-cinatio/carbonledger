"""Decorador das tarefas de tenant.

    @tarefa_de_tenant("coleta.processar_planilha")
    async def processar_planilha(conexao, contexto, *, planilha_id: str) -> None: ...

    processar_planilha.delay(tenant_id=str(tenant), planilha_id=str(planilha))

A tarefa recebe uma conexao asyncpg (nunca SQLAlchemy: conflito de event loop no prefork)
com a transacao aberta e o contexto aplicado; commit no fim, rollback se houver erro.
Argumentos reservados: tenant_id (obrigatorio), empresas e usuario_id (opcionais).
Sem empresas, o escopo e o tenant inteiro; com empresas, so as listadas.
"""

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any
from uuid import UUID

import structlog

from app.core.config import obter_configuracao
from app.core.tenancy import ContextoTenant, Escopo, conexao_de_tenant
from app.workers.celery_app import celery_app
from app.workers.registro import TAREFAS_DE_TENANT, TarefaSemTenant, eh_global

type FuncaoDeTenant[R] = Callable[..., Awaitable[R]]


def _uuid(nome: str, campo: str, valor: Any) -> UUID:
    try:
        return UUID(str(valor))
    except ValueError:
        raise TarefaSemTenant(f"{nome}: {campo} invalido") from None


def contexto_da_tarefa(nome: str, kwargs: dict[str, Any]) -> ContextoTenant:
    """Tira de kwargs os argumentos reservados e monta o contexto."""
    tenant_id = kwargs.pop("tenant_id", None)
    if tenant_id is None:
        raise TarefaSemTenant(f"{nome}: tenant_id e obrigatorio")
    empresas = kwargs.pop("empresas", None)
    usuario_id = kwargs.pop("usuario_id", None)
    return ContextoTenant(
        tenant_id=_uuid(nome, "tenant_id", tenant_id),
        escopo=Escopo.TENANT if empresas is None else Escopo.EMPRESAS,
        empresas=frozenset(_uuid(nome, "empresas", e) for e in (empresas or [])),
        usuario_id=None if usuario_id is None else _uuid(nome, "usuario_id", usuario_id),
    )


async def _rodar[R](
    funcao: FuncaoDeTenant[R], contexto: ContextoTenant, kwargs: dict[str, Any]
) -> R:
    async with conexao_de_tenant(obter_configuracao().database_url, contexto) as conexao:
        return await funcao(conexao, contexto, **kwargs)


def tarefa_de_tenant[R](nome: str, **opcoes: Any) -> Callable[[FuncaoDeTenant[R]], Any]:
    if eh_global(nome):
        raise ValueError(f"{nome} esta na lista de tarefas globais")

    def decorador(funcao: FuncaoDeTenant[R]) -> Any:
        @wraps(funcao)
        def executar(*args: Any, **kwargs: Any) -> R:
            if args:
                raise TarefaSemTenant(f"{nome}: tarefa de tenant aceita so argumentos nomeados")
            contexto = contexto_da_tarefa(nome, kwargs)
            structlog.contextvars.bind_contextvars(tenant_id=str(contexto.tenant_id), tarefa=nome)
            try:
                return asyncio.run(_rodar(funcao, contexto, kwargs))
            finally:
                structlog.contextvars.unbind_contextvars("tenant_id", "tarefa")

        TAREFAS_DE_TENANT.add(nome)
        return celery_app.task(name=nome, **opcoes)(executar)

    return decorador
