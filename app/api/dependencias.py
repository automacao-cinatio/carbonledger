"""Dependencias comuns das rotas: contexto de tenant e sessao com RLS.

O contexto chega em request.state.contexto_tenant, preenchido pela autenticacao (P04).
Sem ele, toda rota de tenant responde 404: falha fechada.
"""

from collections.abc import AsyncIterator
from typing import Annotated, Any

import structlog
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.erros import AcessoNegado
from app.core.tenancy import ATRIBUTO_REQUISICAO, ContextoTenant, sessao_de_tenant


def obter_contexto(request: Request) -> ContextoTenant:
    contexto = getattr(request.state, ATRIBUTO_REQUISICAO, None)
    if not isinstance(contexto, ContextoTenant):
        raise AcessoNegado
    # O usuario_id nao vai para o log: so o tenant, como pede a regra de logs.
    structlog.contextvars.bind_contextvars(tenant_id=str(contexto.tenant_id))
    return contexto


async def obter_sessao(
    request: Request,
    contexto: Annotated[ContextoTenant, Depends(obter_contexto)],
) -> AsyncIterator[AsyncSession]:
    fabrica: async_sessionmaker[AsyncSession] = request.app.state.fabrica_sessoes
    async with sessao_de_tenant(fabrica, contexto) as sessao:
        yield sessao


async def conexao_asyncpg(sessao: AsyncSession) -> Any:
    """Conexao asyncpg da transacao da sessao, com o contexto de tenant ja aplicado.

    Para as funcoes que tambem rodam nos workers (asyncpg direto): calculo e fechamento.
    """
    conexao = await sessao.connection()
    bruta = await conexao.get_raw_connection()
    return bruta.driver_connection


Contexto = Annotated[ContextoTenant, Depends(obter_contexto)]

# scope="function": o commit acontece antes da resposta sair. Se falhar, o cliente recebe
# erro em vez de um 200 de uma gravacao que nao aconteceu.
SessaoTenant = Annotated[AsyncSession, Depends(obter_sessao, scope="function")]
