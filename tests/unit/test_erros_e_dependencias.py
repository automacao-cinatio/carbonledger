from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.api.dependencias import Contexto
from app.core.erros import CORPO_NAO_ENCONTRADO, AcessoNegado, registrar_tratadores
from app.core.tenancy import ATRIBUTO_REQUISICAO, ContextoTenant, Escopo

TENANT = UUID("11111111-1111-4111-8111-111111111111")


def _app() -> FastAPI:
    app = FastAPI()
    registrar_tratadores(app)

    @app.middleware("http")
    async def contexto_de_teste(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.headers.get("x-teste-tenant"):
            contexto = ContextoTenant(
                tenant_id=UUID(request.headers["x-teste-tenant"]), escopo=Escopo.TENANT
            )
            setattr(request.state, ATRIBUTO_REQUISICAO, contexto)
        return await call_next(request)

    @app.get("/negado")
    async def negado() -> None:
        raise AcessoNegado

    @app.get("/proibido")
    async def proibido() -> None:
        raise HTTPException(status_code=403, detail="Sem permissao para a empresa X")

    @app.get("/invalido")
    async def invalido() -> None:
        raise HTTPException(status_code=400, detail="Campo obrigatorio")

    @app.get("/meu-tenant")
    async def meu_tenant(contexto: Contexto) -> dict[str, str]:
        return {"tenant_id": str(contexto.tenant_id)}

    return app


cliente = TestClient(_app())


def test_acesso_negado_vira_404_generico() -> None:
    resposta = cliente.get("/negado")
    assert resposta.status_code == 404
    assert resposta.json() == CORPO_NAO_ENCONTRADO


def test_403_vira_404_sem_vazar_o_detalhe() -> None:
    resposta = cliente.get("/proibido")
    assert resposta.status_code == 404
    assert resposta.json() == CORPO_NAO_ENCONTRADO


def test_rota_inexistente_tem_o_mesmo_corpo() -> None:
    resposta = cliente.get("/nao-existe")
    assert resposta.status_code == 404
    assert resposta.json() == CORPO_NAO_ENCONTRADO


def test_outros_erros_seguem_normais() -> None:
    resposta = cliente.get("/invalido")
    assert resposta.status_code == 400
    assert resposta.json() == {"detail": "Campo obrigatorio"}


def test_rota_de_tenant_sem_contexto_responde_404() -> None:
    resposta = cliente.get("/meu-tenant")
    assert resposta.status_code == 404
    assert resposta.json() == CORPO_NAO_ENCONTRADO


def test_rota_de_tenant_com_contexto() -> None:
    resposta = cliente.get("/meu-tenant", headers={"x-teste-tenant": str(TENANT)})
    assert resposta.status_code == 200
    assert resposta.json() == {"tenant_id": str(TENANT)}
