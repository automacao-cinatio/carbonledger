from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import saude
from app.core.config import obter_configuracao
from app.core.db import criar_motor
from app.core.erros import registrar_tratadores
from app.core.logs import configurar_logs
from app.core.requisicao import MiddlewareRequestId
from app.core.tenancy import SessaoTenant


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI) -> AsyncIterator[None]:
    motor = criar_motor(obter_configuracao().database_url)
    app.state.fabrica_sessoes = async_sessionmaker(
        motor, sync_session_class=SessaoTenant, expire_on_commit=False
    )
    try:
        yield
    finally:
        await motor.dispose()


def criar_app() -> FastAPI:
    configurar_logs(obter_configuracao().log_nivel)
    app = FastAPI(title="CarbonLedger", docs_url=None, redoc_url=None, lifespan=ciclo_de_vida)
    app.add_middleware(MiddlewareRequestId)
    registrar_tratadores(app)
    app.include_router(saude.router)
    return app


app = criar_app()
