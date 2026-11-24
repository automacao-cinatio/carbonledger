"""Login de ponta a ponta: Postgres, Redis e o provedor OIDC simulado, todos reais.

O navegador e simulado em duas partes: o TestClient fala com o app, e o httpx fala com o
provedor (preenche o formulario de login do mock com o sujeito e as claims do teste).
"""

import asyncio
import re
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
from fastapi.testclient import TestClient

from app.cli import criar_tenant
from app.core.config import obter_configuracao
from app.core.erros import CORPO_NAO_ENCONTRADO
from tests.isolation.conftest import Banco
from tests.isolation.portal import COOKIE_SESSAO, entrar

pytestmark = pytest.mark.integration


def _link_do_email(corpo: str) -> str:
    achado = re.search(r"http://testserver(/convite/\S+/dev)", corpo)
    assert achado, corpo
    return achado.group(1)


async def _criar_empresas(banco: Banco, tenant_id: UUID, *nomes: str) -> list[UUID]:
    ids = [uuid4() for _ in nomes]
    grupo = uuid4()
    admin = await asyncpg.connect(banco.dsn_admin)
    try:
        await admin.execute(
            "INSERT INTO grupo (tenant_id, grupo_id, nome) VALUES ($1, $2, 'Grupo E2E')",
            tenant_id,
            grupo,
        )
        await admin.executemany(
            "INSERT INTO empresa (tenant_id, empresa_id, grupo_id, nome) VALUES ($1, $2, $3, $4)",
            [(tenant_id, i, grupo, n) for i, n in zip(ids, nomes, strict=True)],
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
    assert volta.headers["location"] == "/"
    sessao = cliente.get("/sessao").json()
    assert sessao["tenant_atual"] == str(tenant_id)
    assert sessao["perfil"] == "administrador"
    assert sessao["usuario"]["email"] == "dona@e2e.example"
    csrf = {"X-CSRF-Token": sessao["csrf"]}
    assert cliente.get("/teste-login/empresas").json() == ["E2E Norte", "E2E Sul"]

    # Telas: inicio com seletor de empresa; a escolha exige CSRF e fica na sessao.
    inicio = cliente.get("/", headers={"accept": "text/html"})
    assert inicio.status_code == 200
    assert "Olá, Dona" in inicio.text and "Cliente E2E" in inicio.text
    assert inicio.text.index("E2E Norte") < inicio.text.index("E2E Sul")
    assert f'<meta name="csrf-token" content="{sessao["csrf"]}">' in inicio.text
    assert cliente.post("/sessao/empresa", data={"empresa_id": str(sul)}).status_code == 404
    escolha = cliente.post("/sessao/empresa", data={"empresa_id": str(sul)}, headers=csrf)
    assert escolha.status_code == 204
    assert escolha.headers["hx-refresh"] == "true"
    assert f'value="{sul}" selected' in cliente.get("/").text
    alheia = cliente.post("/sessao/empresa", data={"empresa_id": str(uuid4())}, headers=csrf)
    assert alheia.status_code == 404

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
    saida = cliente.post("/sair", headers={**csrf, "HX-Request": "true"})
    assert saida.status_code == 204
    assert saida.headers["hx-redirect"] == "/entrar"
    assert cliente.get("/sessao").status_code == 404
    assert cliente.get("/", follow_redirects=False).headers["location"] == "/entrar"

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


def test_pessoa_com_dois_clientes_escolhe_na_tela(app_cliente: TestClient) -> None:
    email = "consultora@dois.example"
    tenant_1, token_1 = asyncio.run(criar_tenant(obter_configuracao(), "Cliente Um", email))
    tenant_2, token_2 = asyncio.run(criar_tenant(obter_configuracao(), "Cliente Dois", email))
    entrar(app_cliente, f"/convite/{token_1}/dev", sujeito="consultora", email=email)
    entrar(app_cliente, f"/convite/{token_2}/dev", sujeito="consultora", email=email)

    # Login sem convite: dois clientes, entao nenhum abre sozinho.
    volta = entrar(app_cliente, "/entrar/dev", sujeito="consultora", email=email)
    assert volta.status_code == 303
    assert app_cliente.get("/", follow_redirects=False).headers["location"] == "/clientes"
    tela = app_cliente.get("/clientes")
    assert "Cliente Um" in tela.text and "Cliente Dois" in tela.text

    csrf = {"X-CSRF-Token": app_cliente.get("/sessao").json()["csrf"]}
    aberto = app_cliente.post(f"/clientes/{tenant_2}", headers=csrf)
    assert aberto.status_code == 204
    assert aberto.headers["hx-redirect"] == "/"
    assert app_cliente.get("/sessao").json()["tenant_atual"] == str(tenant_2)
    assert "Cliente Dois" in app_cliente.get("/").text
    assert "Trocar de cliente" in app_cliente.get("/").text
    assert app_cliente.post(f"/clientes/{uuid4()}", headers=csrf).status_code == 404
    assert tenant_1 != tenant_2
