"""Tarefa de relatorio: gera, assina e guarda o PDF de um pedido (asyncpg direto, com tenant_id).

As dependencias (chaves, S3 e carimbo do tempo) saem da configuracao, como no app; os testes
trocam `dependencias` por fakes.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from pyhanko.sign import timestamps

from app.core.armazenamento import ArmazemObjetos, ArmazemS3
from app.core.config import obter_configuracao
from app.core.keys import ProvedorChaves, provedor_da_configuracao
from app.core.tenancy import ContextoTenant
from app.modules.relatorios import pedidos, relatorio
from app.workers.tarefas import tarefa_de_tenant

type Dependencias = tuple[ProvedorChaves | None, ArmazemObjetos, timestamps.TimeStamper | None]


def _da_configuracao() -> Dependencias:
    configuracao = obter_configuracao()
    # Sem chave da plataforma o pedido sai como falhou (pedidos.processar), nao fica na fila.
    return (
        provedor_da_configuracao(configuracao),
        ArmazemS3(configuracao),
        relatorio.carimbadora_da_configuracao(configuracao),
    )


dependencias: Callable[[], Dependencias] = _da_configuracao


@tarefa_de_tenant("relatorios.gerar_pedido")
async def gerar_pedido(conexao: Any, contexto: ContextoTenant, *, pedido_id: str) -> str:
    provedor, armazem, carimbadora = dependencias()
    return await pedidos.processar(
        conexao,
        tenant_id=contexto.tenant_id,
        pedido_id=UUID(pedido_id),
        provedor=provedor,
        armazem=armazem,
        carimbadora=carimbadora,
    )
