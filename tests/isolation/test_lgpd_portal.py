"""LGPD de ponta a ponta: meus dados, download, pedido de exclusao e anonimizacao pela
plataforma (linha de comando), com a trilha mantendo so o id."""

import asyncio
import json
import re
from typing import Any
from urllib.parse import unquote
from uuid import UUID

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.cli import criar_tenant
from app.core.config import obter_configuracao
from app.modules.lgpd import servico as lgpd
from tests.isolation.conftest import Banco
from tests.isolation.portal import csrf, entrar

pytestmark = pytest.mark.integration

NEGADO = asyncpg.exceptions.InsufficientPrivilegeError


def _aviso(resposta: Any) -> str:
    assert resposta.status_code == 204, resposta.text
    return unquote(resposta.headers["x-aviso"])


async def _anonimizar(banco: Banco, usuario_id: UUID) -> tuple[list[Any], list[UUID]]:
    conexao = await asyncpg.connect(banco.dsn_app)
    try:
        pendentes = await lgpd.pendentes(conexao)
        tenants = await lgpd.anonimizar(conexao, usuario_id, "Operadora da plataforma")
        with pytest.raises(lgpd.PessoaJaAnonimizada):
            await lgpd.anonimizar(conexao, usuario_id, "Operadora da plataforma")
        with pytest.raises(NEGADO):
            await conexao.fetch("SELECT * FROM lgpd_pedido")
        return pendentes, tenants
    finally:
        await conexao.close()


async def _estado(banco: Banco, usuario_id: UUID, tenant_id: UUID) -> dict[str, Any]:
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        return {
            "usuario": await conexao.fetchrow(
                "SELECT nome, email FROM usuario WHERE usuario_id = $1", usuario_id
            ),
            "identidades": await conexao.fetchval(
                "SELECT count(*) FROM identidade WHERE usuario_id = $1", usuario_id
            ),
            "ativo": await conexao.fetchval(
                "SELECT ativo FROM membro WHERE usuario_id = $1 AND tenant_id = $2",
                usuario_id,
                tenant_id,
            ),
            "empresas": await conexao.fetchval(
                "SELECT count(*) FROM membro_empresa WHERE usuario_id = $1", usuario_id
            ),
            "pedido": await conexao.fetchrow(
                "SELECT executado_em, executado_por FROM lgpd_pedido WHERE usuario_id = $1",
                usuario_id,
            ),
            "trilha": await conexao.fetch(
                "SELECT acao, usuario_id, dados::text AS dados FROM audit_log"
                " WHERE tenant_id = $1 ORDER BY seq",
                tenant_id,
            ),
        }
    finally:
        await conexao.close()


def test_roteiro_lgpd(app_cliente: TestClient, banco: Banco, emails: list[dict[str, str]]) -> None:
    cliente = app_cliente
    tenant_id, token = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Titular", "dona@titular.example")
    )
    entrar(cliente, f"/convite/{token}/dev", sujeito="dona-titular", email="dona@titular.example")
    cabecalho = csrf(cliente)
    cliente.post("/organizacao/grupos", data={"nome": "Grupo T"}, headers=cabecalho)
    grupo = re.search(  # type: ignore[union-attr]
        r"/organizacao/grupos/([0-9a-f-]{36})/editar", cliente.get("/organizacao").text
    ).group(1)
    empresa_id = cliente.post(
        "/organizacao/empresas",
        data={"grupo_id": grupo, "cnpj": "11.222.333/0001-81", "nome": "Titular S.A.",
              "uf": "SP", "municipio": "Campinas"},
        headers=cabecalho,
    ).headers["hx-redirect"].rsplit("/", 1)[-1]  # fmt: skip
    cliente.post(
        "/usuarios/convites",
        data={"email": "colab@titular.example", "perfil": "colaborador", "empresas": empresa_id},
        headers=cabecalho,
    )
    link = re.search(r"http://testserver(/convite/\S+/dev)", emails[-1]["corpo"]).group(1)  # type: ignore[union-attr]

    # A pessoa ve o que a plataforma guarda sobre ela e baixa em JSON.
    entrar(cliente, link, sujeito="colab-titular", email="colab@titular.example")
    assert 'href="/meus-dados"' in cliente.get("/").text
    pagina = cliente.get("/meus-dados").text
    assert "colab@titular.example" in pagina and "Cliente Titular" in pagina
    arquivo = cliente.get("/meus-dados/arquivo.json")
    assert arquivo.status_code == 200
    assert "no-store" in arquivo.headers["cache-control"]
    assert "attachment" in arquivo.headers["content-disposition"]
    dados = json.loads(arquivo.content)
    assert dados["pessoa"]["email"] == "colab@titular.example"
    assert [a["tenant_nome"] for a in dados["acessos"]] == ["Cliente Titular"]
    assert dados["pedidos_lgpd"] == []
    usuario_id = UUID(dados["pessoa"]["id"])

    # Pedido de exclusao: so com o e-mail digitado; repetido nao duplica.
    cabecalho = csrf(cliente)
    errado = cliente.post("/meus-dados/exclusao", data={"confirmacao": "x"}, headers=cabecalho)
    assert errado.status_code == 422
    assert {e["campo"] for e in errado.json()["erros"]} == {"confirmacao"}
    pedido = cliente.post(
        "/meus-dados/exclusao", data={"confirmacao": "Colab@Titular.example "}, headers=cabecalho
    )
    assert "Pedido de exclusão registrado" in _aviso(pedido)
    cliente.post(
        "/meus-dados/exclusao", data={"confirmacao": "colab@titular.example"}, headers=cabecalho
    )
    pagina = cliente.get("/meus-dados").text
    assert "Pedido em andamento" in pagina and "Aguardando a plataforma" in pagina
    assert len(json.loads(cliente.get("/meus-dados/arquivo.json").content)["pedidos_lgpd"]) == 1

    # A plataforma executa pela linha de comando, uma vez so.
    pendentes, tenants = asyncio.run(_anonimizar(banco, usuario_id))
    assert [p["usuario_id"] for p in pendentes if p["usuario_id"] == usuario_id] == [usuario_id]
    assert tenants == [tenant_id]

    estado = asyncio.run(_estado(banco, usuario_id, tenant_id))
    assert estado["usuario"]["nome"] == "Pessoa anonimizada"
    assert "titular" not in estado["usuario"]["email"]
    assert estado["identidades"] == 0
    assert estado["ativo"] is False and estado["empresas"] == 0
    assert estado["pedido"]["executado_por"] == "Operadora da plataforma"
    # Nada some: a trilha continua, com o id e sem nome ou e-mail.
    acoes = [r["acao"] for r in estado["trilha"]]
    assert acoes[-1] == "lgpd.titular_anonimizado"
    assert all("colab@titular" not in r["dados"] for r in estado["trilha"])

    # O mesmo login volta como pessoa nova, sem acesso ao cliente.
    entrar(cliente, "/entrar/dev", sujeito="colab-titular", email="colab@titular.example")
    assert cliente.get("/organizacao").status_code == 404

    # A dona ve a pessoa anonimizada, desativada.
    entrar(cliente, "/entrar/dev", sujeito="dona-titular", email="dona@titular.example")
    usuarios = cliente.get("/usuarios").text
    assert "Pessoa anonimizada" in usuarios and "colab@titular.example" not in usuarios
