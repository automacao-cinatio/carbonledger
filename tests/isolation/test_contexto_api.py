"""Contexto de tenant na API, com o app real, o pool real e o RLS do banco."""

from collections.abc import Iterator
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import text
from structlog.testing import capture_logs

from app.api.dependencias import Contexto, SessaoTenant
from app.core.config import obter_configuracao
from app.core.erros import CORPO_NAO_ENCONTRADO
from app.core.tenancy import ATRIBUTO_REQUISICAO, ContextoTenant, Escopo
from app.main import criar_app
from tests.isolation.conftest import Banco, Dados

pytestmark = pytest.mark.integration

rotas = APIRouter(prefix="/teste")


class FalhaProposital(RuntimeError):
    pass


@rotas.get("/empresas")
async def listar(sessao: SessaoTenant) -> list[str]:
    linhas = await sessao.execute(text("SELECT empresa_id FROM empresa ORDER BY empresa_id"))
    return [str(e) for e in linhas.scalars()]


@rotas.post("/empresas")
async def criar(sessao: SessaoTenant, contexto: Contexto) -> dict[str, object]:
    nova = uuid4()
    await sessao.execute(
        text("INSERT INTO empresa (tenant_id, empresa_id, nome) VALUES (:t, :e, 'Teste API')"),
        {"t": contexto.tenant_id, "e": nova},
    )
    # Commit no meio: a transacao seguinte precisa continuar com o contexto.
    await sessao.commit()
    visivel = await sessao.scalar(
        text("SELECT count(*) FROM empresa WHERE empresa_id = :e"), {"e": nova}
    )
    return {"empresa_id": str(nova), "visivel_apos_commit": visivel}


@rotas.post("/empresas/falha")
async def criar_e_falhar(sessao: SessaoTenant, contexto: Contexto, request: Request) -> None:
    nova = UUID(request.headers["x-teste-nova"])
    await sessao.execute(
        text("INSERT INTO empresa (tenant_id, empresa_id, nome) VALUES (:t, :e, 'Falha')"),
        {"t": contexto.tenant_id, "e": nova},
    )
    raise FalhaProposital


@rotas.get("/sem-contexto")
async def sem_contexto(request: Request) -> dict[str, int]:
    """Usa o pool direto, sem contexto: mostra o que sobrou na conexao reaproveitada."""
    async with request.app.state.fabrica_sessoes() as sessao:
        total = await sessao.scalar(text("SELECT count(*) FROM empresa"))
        tenant = await sessao.scalar(text("SELECT current_setting('app.tenant_id', true)"))
    return {"total": int(total or 0), "tenant_configurado": int(bool(tenant))}


def _app_de_teste() -> FastAPI:
    app = criar_app()
    app.include_router(rotas)

    @app.middleware("http")
    async def contexto_por_cabecalho(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Faz o papel da autenticacao (P04) so neste teste."""
        tenant = request.headers.get("x-teste-tenant")
        if tenant:
            empresas = request.headers.get("x-teste-empresas")
            contexto = ContextoTenant(
                tenant_id=UUID(tenant),
                escopo=Escopo.TENANT if empresas is None else Escopo.EMPRESAS,
                empresas=frozenset(UUID(e) for e in (empresas or "").split(",") if e),
            )
            setattr(request.state, ATRIBUTO_REQUISICAO, contexto)
        return await call_next(request)

    return app


@pytest.fixture
def cliente(banco: Banco, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", banco.dsn_app)
    obter_configuracao.cache_clear()
    with TestClient(_app_de_teste(), raise_server_exceptions=False) as cliente:
        yield cliente
    obter_configuracao.cache_clear()


def _cab(tenant: UUID, *empresas: UUID, escopo_empresas: bool = False) -> dict[str, str]:
    cabecalhos = {"x-teste-tenant": str(tenant)}
    if escopo_empresas:
        cabecalhos["x-teste-empresas"] = ",".join(str(e) for e in empresas)
    return cabecalhos


async def _apagar_empresas(banco: Banco, *, empresa_id: UUID | None = None, nome: str) -> None:
    """Limpa o que o teste gravou, para nao contaminar a semente dos outros testes."""
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        await conexao.execute(
            "DELETE FROM empresa WHERE nome = $1 OR empresa_id = $2", nome, empresa_id
        )
    finally:
        await conexao.close()


def test_sem_contexto_responde_404(cliente: TestClient) -> None:
    resposta = cliente.get("/teste/empresas")
    assert resposta.status_code == 404
    assert resposta.json() == CORPO_NAO_ENCONTRADO


def test_cada_tenant_ve_so_as_suas_empresas(cliente: TestClient, dados: Dados) -> None:
    a = cliente.get("/teste/empresas", headers=_cab(dados.tenant_a))
    b = cliente.get("/teste/empresas", headers=_cab(dados.tenant_b))
    assert a.json() == sorted([str(dados.empresa_a1), str(dados.empresa_a2)])
    assert b.json() == sorted([str(dados.empresa_b1), str(dados.empresa_b2)])


def test_escopo_de_empresas_na_api(cliente: TestClient, dados: Dados) -> None:
    resposta = cliente.get(
        "/teste/empresas",
        headers=_cab(dados.tenant_a, dados.empresa_a2, dados.empresa_b1, escopo_empresas=True),
    )
    assert resposta.json() == [str(dados.empresa_a2)]


def test_conexao_volta_ao_pool_sem_contexto(cliente: TestClient, dados: Dados) -> None:
    for _ in range(3):
        assert cliente.get("/teste/empresas", headers=_cab(dados.tenant_a)).status_code == 200
        assert cliente.get("/teste/sem-contexto").json() == {"total": 0, "tenant_configurado": 0}


async def test_commit_no_meio_mantem_o_contexto(
    cliente: TestClient, banco: Banco, dados: Dados
) -> None:
    try:
        resposta = cliente.post("/teste/empresas", headers=_cab(dados.tenant_a))
        assert resposta.status_code == 200
        corpo = resposta.json()
        assert corpo["visivel_apos_commit"] == 1
        empresas_b = cliente.get("/teste/empresas", headers=_cab(dados.tenant_b)).json()
        assert corpo["empresa_id"] not in empresas_b
    finally:
        await _apagar_empresas(banco, nome="Teste API")


async def test_erro_desfaz_a_gravacao(cliente: TestClient, banco: Banco, dados: Dados) -> None:
    nova = uuid4()
    resposta = cliente.post(
        "/teste/empresas/falha", headers={**_cab(dados.tenant_a), "x-teste-nova": str(nova)}
    )
    assert resposta.status_code == 500
    admin = await asyncpg.connect(banco.dsn_admin)
    try:
        assert await admin.fetchval("SELECT count(*) FROM empresa WHERE empresa_id = $1", nova) == 0
    finally:
        await admin.close()
        await _apagar_empresas(banco, empresa_id=nova, nome="Falha")


def test_log_da_requisicao_leva_tenant_id(cliente: TestClient, dados: Dados) -> None:
    with capture_logs() as registros:
        cliente.get("/teste/empresas", headers=_cab(dados.tenant_a))
    requisicoes = [r for r in registros if r.get("event") == "requisicao"]
    assert requisicoes
    assert requisicoes[-1]["tenant_id"] == str(dados.tenant_a)
    assert "usuario_id" not in requisicoes[-1]
