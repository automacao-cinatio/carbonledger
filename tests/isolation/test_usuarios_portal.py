"""Usuarios e acessos, inicio com a jornada e limite de login, pelo portal com login real."""

import asyncio
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.cli import criar_tenant
from app.core.config import obter_configuracao
from app.main import criar_app
from tests.isolation.portal import AntivirusFalso, ArmazemMemoria, csrf, entrar

pytestmark = pytest.mark.integration


def _aviso(resposta: Any) -> str:
    assert resposta.status_code == 204, resposta.text
    return unquote(resposta.headers["x-aviso"])


def _campos(resposta: Any) -> set[str]:
    assert resposta.status_code == 422, resposta.text
    return {e["campo"] for e in resposta.json()["erros"]}


def test_roteiro_de_usuarios_e_acessos(
    app_cliente: TestClient, emails: list[dict[str, str]]
) -> None:
    cliente = app_cliente
    _tenant, token = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Pessoas", "dona@pessoas.example")
    )
    entrar(cliente, f"/convite/{token}/dev", sujeito="dona-pessoas", email="dona@pessoas.example")
    cabecalho = csrf(cliente)

    # Inicio sem empresa: o proximo passo e a organizacao.
    assert "Cadastre os grupos, as empresas" in cliente.get("/").text
    cliente.post("/organizacao/grupos", data={"nome": "Grupo P"}, headers=cabecalho)
    grupo = re.search(  # type: ignore[union-attr]
        r"/organizacao/grupos/([0-9a-f-]{36})/editar", cliente.get("/organizacao").text
    ).group(1)
    criada = cliente.post(
        "/organizacao/empresas",
        data={
            "grupo_id": grupo,
            "cnpj": "11.222.333/0001-81",
            "nome": "Pessoas S.A.",
            "uf": "SP",
            "municipio": "Campinas",
        },
        headers=cabecalho,
    )
    empresa_id = criada.headers["hx-redirect"].rsplit("/", 1)[-1]

    # Inicio com empresa: a jornada mostra as etapas e manda cadastrar as unidades.
    inicio = cliente.get("/").text
    assert "Onde Pessoas S.A. está" in inicio
    assert "Organização: Cadastre as unidades da empresa." in inicio
    for etapa in ("Coleta", "Resultados", "Fechamento", "Verificação"):
        assert etapa in inicio

    # Usuarios: o link aparece so para o administrador.
    assert 'href="/usuarios"' in inicio
    pagina = cliente.get("/usuarios")
    assert pagina.status_code == 200 and "dona@pessoas.example" in pagina.text

    # Convite: e-mail e perfil validados; colaborador precisa de empresa.
    assert _campos(
        cliente.post("/usuarios/convites", data={"email": "x", "perfil": "nada"}, headers=cabecalho)
    ) <= {"email", "perfil"}
    assert _campos(
        cliente.post(
            "/usuarios/convites",
            data={"email": "colab@pessoas.example", "perfil": "colaborador"},
            headers=cabecalho,
        )
    ) == {"empresas"}
    enviado = cliente.post(
        "/usuarios/convites",
        data={"email": "colab@pessoas.example", "perfil": "colaborador", "empresas": empresa_id},
        headers=cabecalho,
    )
    assert _aviso(enviado) == "Convite enviado por e-mail."
    link = re.search(r"http://testserver(/convite/\S+/dev)", emails[-1]["corpo"]).group(1)  # type: ignore[union-attr]
    cliente.post(
        "/usuarios/convites",
        data={"email": "outro@pessoas.example", "perfil": "gestor"},
        headers=cabecalho,
    )
    pagina = cliente.get("/usuarios").text
    assert "Pendente" in pagina and "Revogar o convite de outro@pessoas.example" in pagina
    convite_outro = re.findall(r"/usuarios/convites/([0-9a-f-]{36})/revogar", pagina)
    assert (
        _aviso(cliente.post(f"/usuarios/convites/{convite_outro[0]}/revogar", headers=cabecalho))
        == "Convite revogado."
    )

    # O colaborador entra e nao ve a tela de usuarios.
    entrar(cliente, link, sujeito="colab-pessoas", email="colab@pessoas.example")
    assert cliente.get("/usuarios", headers={"accept": "application/json"}).status_code == 404
    assert 'href="/usuarios"' not in cliente.get("/").text
    assert cliente.get("/organizacao").status_code == 200

    # A dona muda o perfil, desativa (o acesso cai na hora) e reativa.
    entrar(cliente, "/entrar/dev", sujeito="dona-pessoas", email="dona@pessoas.example")
    cabecalho = csrf(cliente)
    pagina = cliente.get("/usuarios").text
    ids = re.findall(r'href="/usuarios/([0-9a-f-]{36})"', pagina)
    membros = {i: cliente.get(f"/usuarios/{i}").text for i in ids}
    dona = next(i for i, t in membros.items() if "Este é você" in t)
    colaborador = next(i for i in ids if i != dona)
    assert "Este é você" in membros[dona] and "Desativar" not in membros[dona]
    assert _campos(
        cliente.post(f"/usuarios/{dona}", data={"perfil": "colaborador"}, headers=cabecalho)
    ) == {"usuario_id"}
    assert "Acesso alterado" in _aviso(
        cliente.post(
            f"/usuarios/{colaborador}",
            data={"perfil": "responsavel", "empresas": empresa_id},
            headers=cabecalho,
        )
    )
    assert "Responsável" in cliente.get("/usuarios").text
    assert "cortado" in _aviso(
        cliente.post(f"/usuarios/{colaborador}/desativar", headers=cabecalho)
    )
    assert "Desativado" in cliente.get("/usuarios").text
    entrar(cliente, "/entrar/dev", sujeito="colab-pessoas", email="colab@pessoas.example")
    # Desativado, entra mas nao abre o cliente: sem acesso, a tela responde 404.
    assert cliente.get("/organizacao").status_code == 404
    assert "Você não tem acesso ativo" in cliente.get("/clientes").text
    entrar(cliente, "/entrar/dev", sujeito="dona-pessoas", email="dona@pessoas.example")
    assert (
        _aviso(cliente.post(f"/usuarios/{colaborador}/reativar", headers=csrf(cliente)))
        == "Pessoa reativada."
    )


@pytest.fixture
def cliente_com_limite(
    app_cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """App novo com limite 3, de um IP so deste teste (o Redis e compartilhado)."""
    monkeypatch.setenv("LOGIN_LIMITE_TENTATIVAS", "3")
    obter_configuracao.cache_clear()
    app = criar_app()
    app.state.armazem = ArmazemMemoria()
    app.state.antivirus = AntivirusFalso()
    with TestClient(app, follow_redirects=False, client=("10.77.0.9", 4000)) as cliente:
        yield cliente
    obter_configuracao.cache_clear()


def test_limite_de_tentativas_de_login(cliente_com_limite: TestClient) -> None:
    cliente = cliente_com_limite
    for _ in range(3):
        assert cliente.get("/entrar/dev").status_code == 303
    bloqueado = cliente.get("/entrar/dev", headers={"accept": "text/html"})
    assert bloqueado.status_code == 429
    assert "Espere 10 minutos" in bloqueado.text
    # Convite e concessao contam no mesmo limite.
    assert cliente.get("/convite/qualquer/dev").status_code == 404  # token invalido nem conta
    assert cliente.get("/entrar/dev", headers={"accept": "application/json"}).status_code == 429
