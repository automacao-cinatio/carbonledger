"""Tarefa de calculo: recalcula um inventario no worker (asyncpg direto, com tenant_id)."""

from typing import Any
from uuid import UUID

from app.core.tenancy import ContextoTenant
from app.modules.calculo import servico
from app.workers.tarefas import tarefa_de_tenant


@tarefa_de_tenant("calculo.calcular_inventario")
async def calcular_inventario(conexao: Any, contexto: ContextoTenant, *, inventario_id: str) -> str:
    execucao = await servico.calcular_inventario(
        conexao, contexto.tenant_id, UUID(inventario_id), contexto.usuario_id
    )
    return str(execucao.calculo_id)
