"""Login de ponta a ponta: Postgres, Redis e o provedor OIDC simulado, todos reais.

O navegador e simulado em duas partes: o TestClient fala com o app, e o httpx fala com o
provedor (preenche o formulario de login do mock com o sujeito e as claims do teste).
"""

import asyncio
import json
import re
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy import text
from testcontainers.community.redis import RedisContainer
from testcontainers.core.container import DockerContainer

from app.api.dependencias import SessaoTenant
from app.cli import criar_tenant
from app.core.config import obter_configuracao
from app.core.erros import CORPO_NAO_ENCONTRADO
from app.main import criar_app
from tests.isolation.conftest import Banco

pytestmark = pytest.mark.integration

IMAGEM_OIDC = "ghcr.io/navikt/mock-oauth2-server:latest"
COOKIE_SESSAO = "cl_sessao"

rotas_teste = APIRouter(prefix="/teste-login")


@rotas_teste.get("/empresas")
async def empresas_visiveis(sessao: SessaoTenant) -> list[str]:
    linhas = await sessao.execute(text("SELECT nome FROM empresa ORDER BY nome"))
    return list(linhas.scalars())


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    with RedisContainer("redis:7") as conteiner:
        host = conteiner.get_container_host_ip()
        yield f"redis://{host}:{conteiner.get_exposed_port(6379)}/0"


@pytest.fixture(scope="module")
def emissor_oidc() -> Iterator[str]:
    conteiner = (
        DockerContainer(IMAGEM_OIDC)
        .with_exposed_ports(8080)
        .with_env("JSON_CONFIG", '{"interactiveLogin": true}')
    )
    with conteiner:
        emissor = (
            f"http://{conteiner.get_container_host_ip()}:{conteiner.get_exposed_port(8080)}/dev"
        )
        limite = time.monotonic() + 90
        while True:
            try:
                if httpx.get(f"{emissor}/.well-known/openid-configuration").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > limite:
                raise RuntimeError("provedor OIDC simulado nao subiu")
            time.sleep(1)
        yield emissor


@pytest.fixture
def emails(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    enviados: list[dict[str, str]] = []

    async def capturar(_configuracao: Any, para: str, assunto: str, corpo: str) -> None:
        enviados.append({"para": para, "assunto": assunto, "corpo": corpo})

    monkeypatch.setattr("app.core.email.enviar_email", capturar)
    return enviados


@pytest.fixture
def app_cliente(
    banco: Banco, redis_url: str, emissor_oidc: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    for nome, valor in {
        "DATABASE_URL": banco.dsn_app,
        "REDIS_URL": redis_url,
        "OIDC_DEV_EMISSOR": emissor_oidc,
        "COOKIE_SEGURO": "false",
        "APP_URL_BASE": "http://testserver",
    }.items():
        monkeypatch.setenv(nome, valor)
    obter_configuracao.cache_clear()
    app = criar_app()
    app.include_router(rotas_teste)
    with TestClient(app, follow_redirects=False) as cliente:
        yield cliente
    obter_configuracao.cache_clear()


def entrar(
    cliente: TestClient,
    inicio: str,
    *,
    sujeito: str,
    email: str | None,
    verificado: bool = True,
) -> httpx.Response:
    """Percorre o fluxo inteiro e devolve a resposta do retorno no app."""
    cliente.cookies.clear()
    ida = cliente.get(inicio)
    assert ida.status_code == 303, ida.text
    claims: dict[str, Any] = {"email_verified": verificado, "name": sujeito.title()}
    if email is not None:
        claims["email"] = email
    no_provedor = httpx.post(
        ida.headers["location"],
        data={"username": sujeito, "claims": json.dumps(claims)},
        follow_redirects=False,
    )
    assert no_provedor.status_code == 302, no_provedor.text
    volta = urlsplit(no_provedor.headers["location"])
    return cliente.get(f"{volta.path}?{volta.query}")


def _link_do_email(corpo: str) -> str:
    achado = re.search(r"http://testserver(/convite/\S+/dev)", corpo)
    assert achado, corpo
    return achado.group(1)


async def _criar_empresas(banco: Banco, tenant_id: UUID, *nomes: str) -> list[UUID]:
    ids = [uuid4() for _ in nomes]
    admin = await asyncpg.connect(banco.dsn_admin)
    try:
        await admin.executemany(
            "INSERT INTO empresa (tenant_id, empresa_id, nome) VALUES ($1, $2, $3)",
            [(tenant_id, i, n) for i, n in zip(ids, nomes, strict=True)],
        )
    finally:
        await admin.close()
    return ids


def test_roteiro_completo_de_acesso(
    app_cliente: TestClient, banco: Banco, emails: list[dict[str, str]]
) -> None:
    cliente = app_cliente
    tenant_id, token = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente E2E", "dona@e2e.example")
    )
    norte, sul = asyncio.run(_criar_empresas(banco, tenant_id, "E2E Norte", "E2E Sul"))

    # 1. A administradora entra pelo convite criado na linha de comando.
    volta = entrar(cliente, f"/convite/{token}/dev", sujeito="dona", email="dona@e2e.example")
    assert volta.status_code == 303
    assert volta.headers["location"] == "/sessao"
    sessao = cliente.get("/sessao").json()
    assert sessao["tenant_atual"] == str(tenant_id)
    assert sessao["perfil"] == "administrador"
    assert sessao["usuario"]["email"] == "dona@e2e.example"
    csrf = {"X-CSRF-Token": sessao["csrf"]}
    assert cliente.get("/teste-login/empresas").json() == ["E2E Norte", "E2E Sul"]

    # 2. O mesmo convite nao serve de novo, nem para outra pessoa.
    reuso = entrar(cliente, f"/convite/{token}/dev", sujeito="outra", email="dona@e2e.example")
    assert reuso.status_code == 404
    assert reuso.json() == CORPO_NAO_ENCONTRADO

    # 3. Escrita sem o token CSRF e recusada; com ele, o convite sai por e-mail.
    entrar(cliente, "/entrar/dev", sujeito="dona", email="dona@e2e.example")
    sessao = cliente.get("/sessao").json()
    csrf = {"X-CSRF-Token": sessao["csrf"]}
    pedido = {"email": "Resp@E2E.example", "perfil": "responsavel", "empresas": [str(norte)]}
    assert cliente.post("/api/convites", json=pedido).status_code == 404
    assert (
        cliente.post("/api/convites", json=pedido, headers={"X-CSRF-Token": "x"}).status_code == 404
    )
    criado = cliente.post("/api/convites", json=pedido, headers=csrf)
    assert criado.status_code == 201, criado.text
    assert criado.json()["email"] == "resp@e2e.example"
    assert [e["para"] for e in emails] == ["resp@e2e.example"]
    link = _link_do_email(emails[0]["corpo"])

    # Regras de perfil: gestor nao leva empresas; empresa de fora nao e aceita.
    invalido = cliente.post(
        "/api/convites",
        json={"email": "g@e2e.example", "perfil": "gestor", "empresas": [str(norte)]},
        headers=csrf,
    )
    assert invalido.status_code == 422
    assert invalido.json()["erros"][0]["campo"] == "empresas"
    alheia = cliente.post(
        "/api/convites",
        json={"email": "x@e2e.example", "perfil": "colaborador", "empresas": [str(uuid4())]},
        headers=csrf,
    )
    assert alheia.status_code == 422

    # 4. Sair encerra a sessao.
    assert cliente.post("/sair", headers=csrf).status_code == 204
    assert cliente.get("/sessao").status_code == 404

    # 5. A responsavel entra pelo link do e-mail e ve so a empresa dela.
    volta = entrar(cliente, link, sujeito="resp", email="resp@e2e.example")
    assert volta.status_code == 303
    assert cliente.get("/sessao").json()["perfil"] == "responsavel"
    assert cliente.get("/teste-login/empresas").json() == ["E2E Norte"]
    assert cliente.get("/api/membros").status_code == 404
    cookie_responsavel = cliente.cookies[COOKIE_SESSAO]

    # 6. A administradora desativa a responsavel; o acesso cai na requisicao seguinte.
    entrar(cliente, "/entrar/dev", sujeito="dona", email="dona@e2e.example")
    csrf = {"X-CSRF-Token": cliente.get("/sessao").json()["csrf"]}
    membros = cliente.get("/api/membros").json()
    responsavel = next(m for m in membros if m["email"] == "resp@e2e.example")
    assert responsavel["empresas"] == [str(norte)]
    proprio = next(m for m in membros if m["email"] == "dona@e2e.example")
    assert (
        cliente.post(f"/api/membros/{proprio['usuario_id']}/desativar", headers=csrf).status_code
        == 422
    )
    alterado = cliente.patch(
        f"/api/membros/{responsavel['usuario_id']}",
        json={"perfil": "colaborador", "empresas": [str(sul)]},
        headers=csrf,
    )
    assert alterado.status_code == 204
    desativado = cliente.post(f"/api/membros/{responsavel['usuario_id']}/desativar", headers=csrf)
    assert desativado.status_code == 204

    cliente.cookies.clear()
    cliente.cookies.set(COOKIE_SESSAO, cookie_responsavel)
    assert cliente.get("/teste-login/empresas").status_code == 404
    assert cliente.get("/sessao").json()["tenant_atual"] is None
    assert cliente.get("/api/auditoria").status_code == 404

    # 7. A trilha guardou cada passo, sem dado pessoal, e a cadeia esta integra.
    entrar(cliente, "/entrar/dev", sujeito="dona", email="dona@e2e.example")
    eventos = cliente.get("/api/auditoria").json()
    acoes = [e["acao"] for e in eventos]
    assert acoes[:3] == ["tenant.criado", "convite.criado", "convite.aceito"]
    for acao in (
        "sessao.entrou",
        "sessao.saiu",
        "membro.alterado",
        "membro.desativado",
    ):
        assert acao in acoes, acao
    assert acoes.count("convite.aceito") == 2
    assert [e["seq"] for e in eventos] == list(range(1, len(eventos) + 1))
    assert "e2e.example" not in str(eventos)
    alteracao = next(e for e in eventos if e["acao"] == "membro.alterado")
    assert alteracao["entidade_id"] == responsavel["usuario_id"]
    assert alteracao["dados"] == {"perfil": "colaborador", "empresas": [str(sul)]}
    assert cliente.get("/api/auditoria/verificacao").json() == {
        "integra": True,
        "eventos": len(eventos),
        "quebra_em": None,
        "motivo": None,
    }
    assert [e["seq"] for e in cliente.get("/api/auditoria?apos=3&limite=2").json()] == [4, 5]


def test_convite_so_vale_para_o_email_convidado(
    app_cliente: TestClient, emails: list[dict[str, str]]
) -> None:
    _tenant, token = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Email", "certo@email.example")
    )
    errado = entrar(
        app_cliente, f"/convite/{token}/dev", sujeito="e1", email="errado@email.example"
    )
    assert errado.status_code == 404
    sem_verificar = entrar(
        app_cliente,
        f"/convite/{token}/dev",
        sujeito="e2",
        email="certo@email.example",
        verificado=False,
    )
    assert sem_verificar.status_code == 404
    sem_email = entrar(app_cliente, f"/convite/{token}/dev", sujeito="e3", email=None)
    assert sem_email.status_code == 404
    # O convite continua valido para a pessoa certa.
    certo = entrar(app_cliente, f"/convite/{token}/dev", sujeito="e4", email="certo@email.example")
    assert certo.status_code == 303


def test_quem_nao_foi_convidado_nao_entra(app_cliente: TestClient) -> None:
    resposta = entrar(app_cliente, "/entrar/dev", sujeito="estranho", email="x@y.example")
    assert resposta.status_code == 404
    assert COOKIE_SESSAO not in app_cliente.cookies


def test_retorno_sem_o_cookie_do_inicio_e_recusado(app_cliente: TestClient) -> None:
    """Login CSRF: o codigo de outra pessoa nao pode abrir sessao neste navegador."""
    ida = app_cliente.get("/entrar/dev")
    no_provedor = httpx.post(
        ida.headers["location"],
        data={"username": "x", "claims": "{}"},
        follow_redirects=False,
    )
    volta = urlsplit(no_provedor.headers["location"])
    app_cliente.cookies.clear()
    assert app_cliente.get(f"{volta.path}?{volta.query}").status_code == 404


def test_provedor_desconhecido_e_token_malformado(app_cliente: TestClient) -> None:
    assert app_cliente.get("/entrar/inexistente").status_code == 404
    assert app_cliente.get("/convite/nao-e-token/dev").status_code == 404
