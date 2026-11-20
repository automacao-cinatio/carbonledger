from fastapi import FastAPI

from app.api import saude
from app.core.config import obter_configuracao
from app.core.logs import configurar_logs
from app.core.requisicao import MiddlewareRequestId


def criar_app() -> FastAPI:
    configurar_logs(obter_configuracao().log_nivel)
    app = FastAPI(title="CarbonLedger", docs_url=None, redoc_url=None)
    app.add_middleware(MiddlewareRequestId)
    app.include_router(saude.router)
    return app


app = criar_app()
