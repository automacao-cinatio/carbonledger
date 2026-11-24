"""Organizacao de ponta a ponta: grupos, empresas e unidades pelo portal, com login real.

Cobre validacao com varios erros por envio, CNPJ alfanumerico, perfis (administrador,
responsavel e colaborador), desativacao e a trilha de auditoria.
"""

import asyncio
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.cli import criar_tenant
from app.core.config import obter_configuracao
from tests.isolation.portal import csrf, entrar

pytestmark = pytest.mark.integration


def _campos_com_erro(resposta: Any) -> set[str]:
    assert resposta.status_code == 422, resposta.text
    return {erro["campo"] for erro in resposta.json()["erros"]}


def _id_do_redirect(resposta: Any) -> str:
    assert resposta.status_code == 204, resposta.text
    return str(resposta.headers["hx-redirect"]).rsplit("/", 1)[-1]


def _link_do_convite(corpo: str) -> str:
    achado = re.search(r"http://testserver(/convite/\S+/dev)", corpo)
    assert achado, corpo
    return achado.group(1)


def test_roteiro_da_organizacao(app_cliente: TestClient, emails: list[dict[str, str]]) -> None:
    cliente = app_cliente
    _tenant, token = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Estrutura", "dona@estrutura.example")
    )
    entrar(
        cliente, f"/convite/{token}/dev", sujeito="dona-estrutura", email="dona@estrutura.example"
    )
    cabecalho = csrf(cliente)

    # 1. Sem grupo, o proximo passo e criar um.
    indice = cliente.get("/organizacao")
    assert indice.status_code == 200
    assert "Crie o primeiro grupo." in indice.text
    assert 'aria-current="page"' in indice.text

    # 2. Grupo: nome obrigatorio e unico (sem diferenciar maiusculas).
    assert _campos_com_erro(
        cliente.post("/organizacao/grupos", data={"nome": " "}, headers=cabecalho)
    ) == {"nome"}
    criado = cliente.post("/organizacao/grupos", data={"nome": "Holding Verde"}, headers=cabecalho)
    assert criado.status_code == 204 and criado.headers["hx-redirect"] == "/organizacao"
    assert _campos_com_erro(
        cliente.post("/organizacao/grupos", data={"nome": "holding verde"}, headers=cabecalho)
    ) == {"nome"}
    indice = cliente.get("/organizacao").text
    grupo_id = re.search(r"/organizacao/grupos/([0-9a-f-]{36})/editar", indice).group(1)  # type: ignore[union-attr]
    assert "Cadastre a primeira empresa." in indice

    # 3. Empresa: todos os erros no mesmo envio.
    ruim = cliente.post(
        "/organizacao/empresas",
        data={
            "grupo_id": "x",
            "cnpj": "11.222.333/0001-82",
            "nome": "",
            "cnae": "25128",
            "uf": "",
            "municipio": "",
        },
        headers=cabecalho,
    )
    assert _campos_com_erro(ruim) == {"grupo_id", "cnpj", "nome", "cnae", "uf", "municipio"}
    dados_empresa = {
        "grupo_id": grupo_id,
        "cnpj": "12.abc.345/01de-35",
        "nome": "Verde Industria S.A.",
        "nome_fantasia": "Verde",
        "cnae": "2512-8/00",
        "uf": "SP",
        "municipio": "Campinas",
    }
    empresa_id = _id_do_redirect(
        cliente.post("/organizacao/empresas", data=dados_empresa, headers=cabecalho)
    )
    repetida = cliente.post(
        "/organizacao/empresas", data={**dados_empresa, "nome": "Outra"}, headers=cabecalho
    )
    assert _campos_com_erro(repetida) == {"cnpj"}
    segunda = _id_do_redirect(
        cliente.post(
            "/organizacao/empresas",
            data={**dados_empresa, "cnpj": "11.222.333/0001-81", "nome": "Azul Comercio Ltda"},
            headers=cabecalho,
        )
    )

    pagina = cliente.get(f"/organizacao/empresas/{empresa_id}").text
    assert "12.ABC.345/01DE-35" in pagina and "Campinas (SP)" in pagina
    assert "Cadastre a primeira unidade desta empresa." in pagina

    # 4. Unidade: participacao entre 0 e 100, controles por caixa de marcacao.
    url_unidades = f"/organizacao/empresas/{empresa_id}/unidades"
    base_unidade = {"nome": "Fabrica Campinas", "uf": "SP", "municipio": "Campinas"}
    assert _campos_com_erro(
        cliente.post(
            url_unidades, data={**base_unidade, "participacao_societaria": "120"}, headers=cabecalho
        )
    ) == {"participacao_societaria"}
    unidade = cliente.post(
        url_unidades,
        data={**base_unidade, "participacao_societaria": "51,5", "controle_operacional": "sim"},
        headers=cabecalho,
    )
    assert unidade.status_code == 204
    pagina = cliente.get(f"/organizacao/empresas/{empresa_id}").text
    assert "51,50" in pagina and ">Sim<" in pagina and ">Não<" in pagina
    unidade_id = re.search(r"/organizacao/unidades/([0-9a-f-]{36})/editar", pagina).group(1)  # type: ignore[union-attr]

    # Edicao: so o campo mudado vai para a trilha.
    salvo = cliente.post(
        f"/organizacao/unidades/{unidade_id}",
        data={**base_unidade, "participacao_societaria": "100", "controle_operacional": "sim"},
        headers=cabecalho,
    )
    assert salvo.status_code == 204
    assert "100,00" in cliente.get(f"/organizacao/empresas/{empresa_id}").text

    # 5. Validacao na hora.
    validacao = cliente.post("/organizacao/validar/cnpj", data={"cnpj": "123"}, headers=cabecalho)
    assert "dígitos finais" in validacao.text

    # 6. Responsavel pela segunda empresa: ve so ela e cuida so das unidades dela.
    convite = cliente.post(
        "/api/convites",
        json={"email": "resp@estrutura.example", "perfil": "responsavel", "empresas": [segunda]},
        headers=cabecalho,
    )
    assert convite.status_code == 201
    convite_colab = cliente.post(
        "/api/convites",
        json={"email": "colab@estrutura.example", "perfil": "colaborador", "empresas": [segunda]},
        headers=cabecalho,
    )
    assert convite_colab.status_code == 201
    links = {e["para"]: _link_do_convite(e["corpo"]) for e in emails}

    entrar(
        cliente,
        links["resp@estrutura.example"],
        sujeito="resp-estrutura",
        email="resp@estrutura.example",
    )
    cab_resp = csrf(cliente)
    indice = cliente.get("/organizacao").text
    assert "Azul Comercio Ltda" in indice and "Verde Industria" not in indice
    assert "Novo grupo" not in indice
    assert cliente.get(f"/organizacao/empresas/{empresa_id}").status_code == 404
    assert cliente.get("/organizacao/empresas/nova").status_code == 404
    assert (
        cliente.post("/organizacao/grupos", data={"nome": "Intruso"}, headers=cab_resp).status_code
        == 404
    )
    propria = cliente.post(
        f"/organizacao/empresas/{segunda}/unidades",
        data={**base_unidade, "nome": "Loja Centro", "participacao_societaria": "100"},
        headers=cab_resp,
    )
    assert propria.status_code == 204
    alheia = cliente.post(
        url_unidades,
        data={**base_unidade, "nome": "Invasao", "participacao_societaria": "100"},
        headers=cab_resp,
    )
    assert alheia.status_code == 404
    assert (
        cliente.post(f"/organizacao/unidades/{unidade_id}/desativar", headers=cab_resp).status_code
        == 404
    )

    # 7. Colaborador: le, nao altera.
    entrar(
        cliente,
        links["colab@estrutura.example"],
        sujeito="colab-estrutura",
        email="colab@estrutura.example",
    )
    cab_colab = csrf(cliente)
    assert cliente.get(f"/organizacao/empresas/{segunda}").status_code == 200
    assert "Nova unidade" not in cliente.get(f"/organizacao/empresas/{segunda}").text
    assert (
        cliente.post(
            f"/organizacao/empresas/{segunda}/unidades",
            data={**base_unidade, "nome": "X", "participacao_societaria": "100"},
            headers=cab_colab,
        ).status_code
        == 404
    )

    # 8. Desativar a empresa: some do seletor e nao recebe unidade nova.
    entrar(cliente, "/entrar/dev", sujeito="dona-estrutura", email="dona@estrutura.example")
    cabecalho = csrf(cliente)
    desativada = cliente.post(f"/organizacao/empresas/{empresa_id}/desativar", headers=cabecalho)
    assert desativada.status_code == 204 and desativada.headers["hx-refresh"] == "true"
    inicio = cliente.get("/").text
    assert f'value="{empresa_id}"' not in inicio and f'value="{segunda}"' in inicio
    assert cliente.get(f"/organizacao/empresas/{empresa_id}/unidades/nova").status_code == 404
    assert "Empresa desativada" in cliente.get(f"/organizacao/empresas/{empresa_id}").text

    # 9. Pagina de erro com sessao: HTML com proximo passo.
    erro = cliente.get(
        "/organizacao/empresas/00000000-0000-4000-8000-000000000000",
        headers={"accept": "text/html"},
    )
    assert erro.status_code == 404 and "Página não encontrada" in erro.text

    # 10. Trilha: cada mudanca registrada, so com nomes de campo, e a cadeia integra.
    eventos = cliente.get("/api/auditoria?limite=500").json()
    acoes = [e["acao"] for e in eventos]
    for acao in (
        "grupo.criado",
        "empresa.criada",
        "unidade.criada",
        "unidade.alterada",
        "empresa.desativada",
    ):
        assert acao in acoes, acao
    alteracao = next(e for e in eventos if e["acao"] == "unidade.alterada")
    assert alteracao["dados"]["campos"] == ["participacao_societaria"]
    assert "Verde" not in str(eventos) and "12ABC" not in str(eventos)
    assert cliente.get("/api/auditoria/verificacao").json()["integra"] is True
