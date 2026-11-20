from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis_async
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import autenticacao, organizacao, saude
from app.core.config import obter_configuracao
from app.core.db import criar_motor
from app.core.erros import registrar_tratadores
from app.core.logs import configurar_logs
from app.core.requisicao import MiddlewareRequestId
from app.core.security.acesso import MiddlewareSessao
from app.core.security.oidc import ClienteOidc, provedores_habilitados
from app.core.security.sessao import ArmazemSessoes
from app.core.tenancy import SessaoTenant

TEMPO_LIMITE_OIDC_S = 10.0


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI) -> AsyncIterator[None]:
    configuracao = obter_configuracao()
    motor = criar_motor(configuracao.database_url)
    app.state.fabrica_sessoes = async_sessionmaker(
        motor, sync_session_class=SessaoTenant, expire_on_commit=False
    )
    cliente_redis = redis_async.from_url(configuracao.redis_url, decode_responses=True)
    app.state.sessoes = ArmazemSessoes(
        cliente_redis,
        inatividade_s=configuracao.sessao_inatividade_min * 60,
        maxima_s=configuracao.sessao_maxima_h * 3600,
    )
    app.state.cookie_seguro = configuracao.cookie_seguro
    app.state.provedores = provedores_habilitados(configuracao)
    http = httpx.AsyncClient(timeout=TEMPO_LIMITE_OIDC_S, follow_redirects=False)
    app.state.oidc = ClienteOidc(http)
    try:
        yield
    finally:
        await http.aclose()
        await cliente_redis.aclose()
        await motor.dispose()


def criar_app() -> FastAPI:
    configurar_logs(obter_configuracao().log_nivel)
    app = FastAPI(title="CarbonLedger", docs_url=None, redoc_url=None, lifespan=ciclo_de_vida)
    # O ultimo adicionado e o mais externo: o request_id envolve a leitura da sessao.
    app.add_middleware(MiddlewareSessao)
    app.add_middleware(MiddlewareRequestId)
    registrar_tratadores(app)
    app.include_router(saude.router)
    app.include_router(autenticacao.router)
    app.include_router(organizacao.router)
    return app


app = criar_app()
