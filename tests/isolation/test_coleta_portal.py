"""Coleta de ponta a ponta pelo portal, com login real, Postgres e Redis de verdade.

Cobre abrir o inventario, cadastrar fonte, lancar, aprovar com outra pessoa, devolver e
corrigir (versao nova), evidencia (tipo, macro, antivirus, hash e download), planilha de
coleta (modelo, erros com linha e coluna, tudo ou nada), faixa de estado e trilha.
O S3 e o ClamAV sao substitutos em memoria (tests/isolation/portal.py).
"""

import asyncio
import csv
import hashlib
import io
import re
from datetime import date
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.cli import criar_tenant
from app.core.config import obter_configuracao
from tests.isolation.conftest import Banco, Dados
from tests.isolation.portal import (
    CONJUNTO_GWP_DE_TESTE,
    carregar_referencia_de_calculo,
    csrf,
    entrar,
)

pytestmark = pytest.mark.integration

ANO = date.today().year - 1
PDF = b"%PDF-1.7\n1 0 obj << >> endobj\ntrailer\n%%EOF\n"
EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def _campos_com_erro(resposta: Any) -> set[str]:
    assert resposta.status_code == 422, resposta.text
    return {erro["campo"] for erro in resposta.json()["erros"]}


def _mensagens(resposta: Any) -> str:
    assert resposta.status_code == 422, resposta.text
    return " ".join(erro["mensagem"] for erro in resposta.json()["erros"])


def _id_do_redirect(resposta: Any) -> str:
    assert resposta.status_code == 204, resposta.text
    return str(resposta.headers["hx-redirect"]).rsplit("/", 1)[-1]


def _aviso(resposta: Any) -> str:
    from urllib.parse import unquote

    assert resposta.status_code == 204, resposta.text
    return unquote(resposta.headers["x-aviso"])


def _link_do_convite(corpo: str) -> str:
    achado = re.search(r"http://testserver(/convite/\S+/dev)", corpo)
    assert achado, corpo
    return achado.group(1)


async def _acoes_da_trilha(banco: Banco, tenant_id: Any) -> list[str]:
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        linhas = await conexao.fetch(
            "SELECT acao, dados::text AS dados FROM audit_log WHERE tenant_id = $1 ORDER BY seq",
            tenant_id,
        )
    finally:
        await conexao.close()
    # Texto livre (observacao, motivo, nome do arquivo) nunca entra na trilha.
    for linha in linhas:
        assert "nota-fiscal" not in linha["dados"] and "nao bate" not in linha["dados"]
    return [linha["acao"] for linha in linhas]


def test_roteiro_da_coleta(
    app_cliente: TestClient, banco: Banco, dados: Dados, emails: list[dict[str, str]]
) -> None:
    cliente = app_cliente
    armazem = cliente.app.state.armazem  # type: ignore[attr-defined]
    antivirus = cliente.app.state.antivirus  # type: ignore[attr-defined]
    tenant_id, token = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Coleta", "dona@coleta.example")
    )
    entrar(cliente, f"/convite/{token}/dev", sujeito="dona-coleta", email="dona@coleta.example")
    cabecalho = csrf(cliente)

    # 0. Sem empresa, a coleta manda para a organizacao.
    assert "Cadastre a empresa e as unidades" in cliente.get("/coleta").text
    cliente.post("/organizacao/grupos", data={"nome": "Grupo Coleta"}, headers=cabecalho)
    grupo_id = re.search(  # type: ignore[union-attr]
        r"/organizacao/grupos/([0-9a-f-]{36})/editar", cliente.get("/organizacao").text
    ).group(1)
    empresa_id = _id_do_redirect(
        cliente.post(
            "/organizacao/empresas",
            data={
                "grupo_id": grupo_id,
                "cnpj": "11.222.333/0001-81",
                "nome": "Coleta Industria S.A.",
                "uf": "SP",
                "municipio": "Campinas",
            },
            headers=cabecalho,
        )
    )
    unidade = cliente.post(
        f"/organizacao/empresas/{empresa_id}/unidades",
        data={
            "nome": "Fabrica Campinas",
            "uf": "SP",
            "municipio": "Campinas",
            "participacao_societaria": "100",
            "controle_operacional": "sim",
        },
        headers=cabecalho,
    )
    assert unidade.status_code == 204
    unidade_id = re.search(  # type: ignore[union-attr]
        r"/organizacao/unidades/([0-9a-f-]{36})/editar",
        cliente.get(f"/organizacao/empresas/{empresa_id}").text,
    ).group(1)

    # 1. Abrir o inventario: ano e abordagem validados; um por empresa e ano.
    coleta = cliente.get("/coleta")
    assert coleta.status_code == 200 and 'aria-current="page"' in coleta.text
    assert "Sem inventário aberto" in coleta.text
    url_inventarios = f"/coleta/empresas/{empresa_id}/inventarios"
    assert _campos_com_erro(
        cliente.post(url_inventarios, data={"ano": "1999", "abordagem": "x"}, headers=cabecalho)
    ) == {"ano", "abordagem"}
    inventario_id = _id_do_redirect(
        cliente.post(
            url_inventarios,
            data={"ano": str(ANO), "abordagem": "controle_operacional"},
            headers=cabecalho,
        )
    )
    assert _campos_com_erro(
        cliente.post(
            url_inventarios,
            data={"ano": str(ANO), "abordagem": "controle_financeiro"},
            headers=cabecalho,
        )
    ) == {"ano"}
    pagina = cliente.get(f"/coleta/inventarios/{inventario_id}").text
    assert f"Inventário {ANO}" in pagina and "Cadastre as fontes de emissão" in pagina

    # 2. Fonte: todos os erros juntos; energia eletrica so mensal; fora da materialidade
    #    exige justificativa.
    url_fontes = f"/coleta/inventarios/{inventario_id}/fontes"
    assert _campos_com_erro(
        cliente.post(url_fontes, data={"material": "sim"}, headers=cabecalho)
    ) == {
        "unidade_id",
        "categoria",
        "unidade_medida",
        "periodicidade",
        "descricao",
    }
    base_fonte = {
        "unidade_id": unidade_id,
        "categoria": "escopo1.combustao_estacionaria",
        "descricao": "Caldeira 1 (diesel)",
        "unidade_medida": "L",
        "periodicidade": "mensal",
        "material": "sim",
    }
    assert _campos_com_erro(
        cliente.post(
            url_fontes,
            data={**base_fonte, "categoria": "escopo2.energia_eletrica", "periodicidade": "anual"},
            headers=cabecalho,
        )
    ) == {"periodicidade"}
    assert _campos_com_erro(
        cliente.post(url_fontes, data={**base_fonte, "material": ""}, headers=cabecalho)
    ) == {"justificativa"}
    assert _campos_com_erro(
        cliente.post(url_fontes, data={**base_fonte, "unidade_medida": "litro"}, headers=cabecalho)
    ) == {"unidade_medida"}
    fonte_id = _id_do_redirect(cliente.post(url_fontes, data=base_fonte, headers=cabecalho))
    fora = _id_do_redirect(
        cliente.post(
            url_fontes,
            data={
                **base_fonte,
                "categoria": "escopo1.fugitivas",
                "descricao": "Extintores de CO2",
                "unidade_medida": "kg",
                "periodicidade": "anual",
                "material": "",
                "justificativa": "Recarga irrelevante, menos de 1 kg no ano.",
            },
            headers=cabecalho,
        )
    )
    pagina_fonte = cliente.get(f"/coleta/fontes/{fonte_id}").text
    assert pagina_fonte.count("Falta lançar") == 12
    assert "Fora da materialidade" in cliente.get(f"/coleta/inventarios/{inventario_id}").text
    assert "Fonte fora da materialidade" in cliente.get(f"/coleta/fontes/{fora}").text

    # 3. Lancar: quantidade em pt-BR; mesmo dado de novo nao cria versao.
    url_jan = f"/coleta/fontes/{fonte_id}/periodos/1"
    assert _campos_com_erro(
        cliente.post(
            url_jan + "/lancamentos",
            data={"quantidade": "1.5", "data_compra": "2000-01-01"},
            headers=cabecalho,
        )
    ) == {"quantidade", "data_compra"}
    dado_jan = {
        "quantidade": "1.234,5",
        "data_compra": f"{ANO}-01-15",
        "observacao": "nota-fiscal 123",
    }
    assert "aguarda a aprovação" in _aviso(
        cliente.post(url_jan + "/lancamentos", data=dado_jan, headers=cabecalho)
    )
    assert "Nada mudou" in _aviso(
        cliente.post(url_jan + "/lancamentos", data=dado_jan, headers=cabecalho)
    )
    pagina = cliente.get(url_jan).text
    assert "1.234,50" in pagina and "Aguardando aprovação" in pagina
    assert "Quem lança o dado não pode aprová-lo" in pagina and "Aprovar o dado" not in pagina
    lancamento_jan = re.search(  # type: ignore[union-attr]
        r"/coleta/lancamentos/([0-9a-f-]{36})/", cliente.get(url_jan).text + url_jan
    )
    assert lancamento_jan is None, "sem botao de decisao para quem lancou"

    # Fevereiro para ser devolvido depois.
    url_fev = f"/coleta/fontes/{fonte_id}/periodos/2"
    cliente.post(url_fev + "/lancamentos", data={"quantidade": "900"}, headers=cabecalho)

    # 4. Responsavel da empresa: aprova o que a dona lancou, devolve com motivo.
    convite = cliente.post(
        "/api/convites",
        json={"email": "resp@coleta.example", "perfil": "responsavel", "empresas": [empresa_id]},
        headers=cabecalho,
    )
    assert convite.status_code == 201
    entrar(
        cliente,
        _link_do_convite(emails[-1]["corpo"]),
        sujeito="resp-coleta",
        email="resp@coleta.example",
    )
    cab_resp = csrf(cliente)
    pagina = cliente.get(url_jan).text
    assert "Aprovar o dado" in pagina
    id_jan = re.search(r"/coleta/lancamentos/([0-9a-f-]{36})/aprovar", pagina).group(1)  # type: ignore[union-attr]
    assert _aviso(cliente.post(f"/coleta/lancamentos/{id_jan}/aprovar", headers=cab_resp)) == (
        "Dado aprovado."
    )
    assert _campos_com_erro(
        cliente.post(f"/coleta/lancamentos/{id_jan}/aprovar", headers=cab_resp)
    ) == {"lancamento"}
    id_fev = re.search(  # type: ignore[union-attr]
        r"/coleta/lancamentos/([0-9a-f-]{36})/devolver", cliente.get(url_fev).text
    ).group(1)
    assert _campos_com_erro(
        cliente.post(
            f"/coleta/lancamentos/{id_fev}/devolver", data={"motivo": ""}, headers=cab_resp
        )
    ) == {"motivo"}
    devolvido = cliente.post(
        f"/coleta/lancamentos/{id_fev}/devolver",
        data={"motivo": "Valor nao bate com a nota"},
        headers=cab_resp,
    )
    assert _aviso(devolvido) == "Dado devolvido para correção."
    # Fonte de outro tenant: 404, como qualquer coisa que nao existe.
    assert cliente.get(f"/coleta/fontes/{dados.fonte_b1}").status_code == 404

    # 5. A dona corrige: versao 2, que volta a pedir aprovacao.
    entrar(cliente, "/entrar/dev", sujeito="dona-coleta", email="dona@coleta.example")
    cabecalho = csrf(cliente)
    pagina = cliente.get(url_fev).text
    assert "Valor nao bate com a nota" in pagina and "Corrigir o dado" in pagina
    inventario = cliente.get(f"/coleta/inventarios/{inventario_id}").text
    assert "Corrija o dado devolvido" in inventario
    cliente.post(url_fev + "/lancamentos", data={"quantidade": "950"}, headers=cabecalho)
    pagina = cliente.get(url_fev).text
    assert "Devolvido para correção" in pagina and "Aguardando aprovação" in pagina

    # 6. Evidencia: tipo pelo conteudo, macro e antivirus recusados; hash e download.
    url_evidencias = f"/coleta/fontes/{fonte_id}/evidencias"

    def enviar(nome: str, conteudo: bytes, periodo: str = "1") -> Any:
        return cliente.post(
            url_evidencias,
            data={"periodo": periodo},
            files={"arquivo": (nome, conteudo, "application/octet-stream")},
            headers=cabecalho,
        )

    assert "macro" in _mensagens(enviar("planilha.xlsm", b"PK\x03\x04qualquer"))
    assert "extensão .pdf" in _mensagens(enviar("nota.png", PDF))
    assert "ameaça" in _mensagens(enviar("virus.txt", EICAR))
    antivirus.fora_do_ar = True
    assert "Tente de novo" in _mensagens(enviar("nota-fiscal.pdf", PDF))
    antivirus.fora_do_ar = False
    assert not armazem.objetos, "nada guardado antes de passar por todas as checagens"
    assert _aviso(enviar("nota-fiscal.pdf", PDF)) == "Evidência guardada."
    assert _aviso(enviar("contas-do-ano.pdf", PDF + b"2", periodo="")) == "Evidência guardada."
    chaves = list(armazem.objetos)
    assert all(chave.startswith(f"tenants/{tenant_id}/empresas/{empresa_id}/") for chave in chaves)
    assert not any("nota" in chave for chave in chaves), "o nome do arquivo nao vai para o S3"
    pagina = cliente.get(f"/coleta/fontes/{fonte_id}").text
    assert hashlib.sha256(PDF).hexdigest()[:12] in pagina
    id_evidencia = re.search(  # type: ignore[union-attr]
        r"/coleta/evidencias/([0-9a-f-]{36})/arquivo", pagina
    ).group(1)
    baixado = cliente.get(f"/coleta/evidencias/{id_evidencia}/arquivo")
    assert baixado.status_code == 200
    assert baixado.content in (PDF, PDF + b"2")
    assert baixado.headers["content-disposition"].startswith("attachment;")

    # 7. Planilha: modelo com 12 linhas da fonte ativa (a anual fora da materialidade tambem
    #    vem, para nada ficar escondido), erros com linha e coluna, tudo ou nada.
    modelo = cliente.get(f"/coleta/inventarios/{inventario_id}/planilha.csv")
    assert modelo.status_code == 200 and modelo.content.startswith("﻿".encode())
    linhas = list(csv.reader(io.StringIO(modelo.content.decode("utf-8-sig")), delimiter=";"))
    cabecalho_csv, corpo = linhas[0], linhas[1:]
    assert cabecalho_csv[0] == "fonte_id" and len(corpo) == 13
    coluna = {nome: i for i, nome in enumerate(cabecalho_csv)}
    janeiro = next(
        linha
        for linha in corpo
        if linha[coluna["fonte_id"]] == fonte_id and linha[coluna["periodo"]] == "01"
    )
    assert janeiro[coluna["quantidade"]] == "1234,5"

    def com(alteracoes: dict[str, str]) -> bytes:
        saida = io.StringIO()
        escritor = csv.writer(saida, delimiter=";")
        escritor.writerow(cabecalho_csv)
        for linha in corpo:
            nova = list(linha)
            if linha[coluna["fonte_id"]] == fonte_id and linha[coluna["periodo"]] in alteracoes:
                nova[coluna["quantidade"]] = alteracoes[linha[coluna["periodo"]]]
            escritor.writerow(nova)
        return saida.getvalue().encode("utf-8")

    url_planilha = f"/coleta/inventarios/{inventario_id}/planilha"

    def importar(conteudo: bytes) -> Any:
        return cliente.post(
            url_planilha,
            files={"arquivo": ("coleta.csv", conteudo, "text/csv")},
            headers=cabecalho,
        )

    erro = _mensagens(importar(com({"03": "300", "04": "1.5", "05": "-2"})))
    assert "Linha 5, coluna quantidade" in erro and "Linha 6, coluna quantidade" in erro
    assert "Aguardando" not in cliente.get(f"/coleta/fontes/{fonte_id}/periodos/3").text
    assert "2 dados gravados" in _aviso(importar(com({"03": "300", "04": "400,25"})))
    assert "nenhum dado novo" in _aviso(importar(com({"03": "300", "04": "400,25"})))
    pagina = cliente.get(f"/coleta/fontes/{fonte_id}/periodos/4").text
    assert "400,25" in pagina and "Planilha" in pagina

    # 8. Faixa de estado: inventario da empresa em foco, com o progresso real.
    inicio = cliente.get("/").text
    assert f"Inventário {ANO}" in inicio and "Em coleta" in inicio
    assert "1 de 12 (8%)" in inicio  # janeiro aprovado; janeiro tem evidencia
    assert "Sem inventário aberto" not in inicio

    # 9. Trilha: cada passo registrado, sem texto livre.
    acoes = asyncio.run(_acoes_da_trilha(banco, tenant_id))
    for acao in (
        "inventario.criado",
        "fonte.criada",
        "lancamento.enviado",
        "lancamento.aprovado",
        "lancamento.devolvido",
        "evidencia.enviada",
        "planilha.importada",
    ):
        assert acao in acoes, acao

    # 10. Calculo do escopo 1 (P09) com a referencia FICTICIA aprovada pela curadoria. So
    #     janeiro esta aprovado: 1.234,5 L de diesel comercial (85/15), controle operacional.
    #     fossil: CO2 2,728245 t; CH4 0,01175244 tCO2e. bio: CH4 0,00155547 tCO2e.
    #     escopo 1 = 2,74155291 tCO2e; fevereiro a dezembro viram pendencia.
    asyncio.run(carregar_referencia_de_calculo(banco.dsn_curadoria))
    asyncio.run(_usar_gwp_de_teste(banco, inventario_id))
    assert "Nenhum cálculo feito" in cliente.get("/resultados").text
    url_fonte = f"/coleta/fontes/{fonte_id}"
    assert "teste_calc.diesel_comercial" in cliente.get(url_fonte + "/editar").text
    assert _campos_com_erro(
        cliente.post(url_fonte, data={**base_fonte, "atividade": "inventada"}, headers=cabecalho)
    ) == {"atividade"}
    salvo = cliente.post(
        url_fonte,
        data={**base_fonte, "atividade": "teste_calc.diesel_comercial"},
        headers=cabecalho,
    )
    assert salvo.status_code == 204
    calculado = cliente.post(f"/resultados/inventarios/{inventario_id}/calcular", headers=cabecalho)
    aviso = _aviso(calculado)
    assert "2,742 tCO₂e" in aviso and "11 pendência(s)" in aviso
    url_calculo = calculado.headers["hx-redirect"]
    pagina = cliente.get(url_calculo).text
    assert "2,742 tCO₂e" in pagina and "Dado não aprovado" in pagina
    assert "Combustão estacionária" in pagina and "CH4" in pagina
    calculo_id = url_calculo.rsplit("/", 1)[-1]
    memoria = cliente.get(f"/resultados/calculos/{calculo_id}/memoria.csv")
    assert memoria.status_code == 200
    linhas_memoria = memoria.content.decode("utf-8-sig").strip().splitlines()
    assert len(linhas_memoria) == 5  # cabecalho e 4 linhas (2 gases x 2 componentes)
    assert "2,728245" in memoria.text and "1049,325" in memoria.text
    conferido = cliente.post(f"/resultados/calculos/{calculo_id}/conferir", headers=cabecalho)
    assert _aviso(conferido).startswith("Conferido")
    assert "calculo.executado" in asyncio.run(_acoes_da_trilha(banco, tenant_id))

    # 11. Fechamento (P11): com pendencias, nao fecha, e a tela diz o que falta.
    pagina = cliente.get("/fechamento").text
    assert "Condições para fechar" in pagina and "Sem pendências" in pagina
    assert "Fechar o inventário" not in pagina
    recusado = cliente.post(
        f"/fechamento/inventarios/{inventario_id}/fechar",
        data={"confirmacao": "Coleta Industria S.A."},
        headers=cabecalho,
    )
    assert "Ainda não dá para fechar" in _mensagens(recusado)

    # 12a. Campanha de coleta (P21): prazo validado; o responsavel da empresa recebe o aviso
    #      com o que falta; encerrar para os lembretes.
    url_campanha = f"/coleta/inventarios/{inventario_id}/campanha"
    assert _campos_com_erro(
        cliente.post(url_campanha, data={"prazo": "2000-01-01"}, headers=cabecalho)
    ) == {"prazo"}
    prazo = date.fromordinal(date.today().toordinal() + 10).isoformat()
    aberta = cliente.post(
        url_campanha, data={"prazo": prazo, "mensagem": "Até sexta."}, headers=cabecalho
    )
    assert "1 pessoa(s) avisada(s)" in _aviso(aberta)
    aviso = emails[-1]
    assert aviso["para"] == "resp@coleta.example" and "Até sexta." in aviso["corpo"]
    assert "Caldeira 1 (diesel): Fevereiro (aguardando aprovação)" in aviso["corpo"]
    pagina = cliente.get(f"/coleta/inventarios/{inventario_id}").text
    assert "Encerrar campanha" in pagina and "Abertura" in pagina
    campanha_id = re.search(r"/coleta/campanhas/([0-9a-f-]{36})/encerrar", pagina).group(1)  # type: ignore[union-attr]
    assert "Os lembretes param" in _aviso(
        cliente.post(f"/coleta/campanhas/{campanha_id}/encerrar", headers=cabecalho)
    )

    # 12. Integridade (P20): tudo o que o roteiro gravou confere; um arquivo adulterado no
    #     armazenamento aparece como falha. A verificacao pedida vai para o worker (aqui, a
    #     tarefa roda logo depois da resposta).
    assert "Nenhuma verificação ainda" in cliente.get("/integridade").text
    assert _aviso(cliente.post("/integridade/verificar", headers=cabecalho)).startswith(
        "Verificação pedida"
    )
    assert "Tudo confere" in cliente.get("/integridade").text
    chave = next(c for c in armazem.objetos if "/evidencias/" in c)
    armazem.objetos[chave] = (b"%PDF-1.7 adulterado", "application/pdf")
    cliente.post("/integridade/verificar", headers=cabecalho)
    pagina = cliente.get("/integridade").text
    assert "Falhas de integridade" in pagina and "SHA-256 diferente do registrado" in pagina


async def _usar_gwp_de_teste(banco: Banco, inventario_id: str) -> None:
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        await conexao.execute(
            "UPDATE inventario SET gwp_conjunto = $1 WHERE inventario_id = $2",
            CONJUNTO_GWP_DE_TESTE,
            inventario_id,
        )
    finally:
        await conexao.close()


def test_fonte_de_unidade_fora_do_limite(app_cliente: TestClient, dados: Dados) -> None:
    """Unidade sem controle operacional num inventario por controle operacional: a fonte dela
    aparece como fora do limite, com aviso, e nao conta no progresso."""
    cliente = app_cliente
    _tenant_id, token = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Limite", "dona@limite.example")
    )
    entrar(cliente, f"/convite/{token}/dev", sujeito="dona-limite", email="dona@limite.example")
    cabecalho = csrf(cliente)
    cliente.post("/organizacao/grupos", data={"nome": "Grupo Limite"}, headers=cabecalho)
    grupo_id = re.search(  # type: ignore[union-attr]
        r"/organizacao/grupos/([0-9a-f-]{36})/editar", cliente.get("/organizacao").text
    ).group(1)
    empresa_id = _id_do_redirect(
        cliente.post(
            "/organizacao/empresas",
            data={
                "grupo_id": grupo_id,
                "cnpj": "11.222.333/0001-81",
                "nome": "Limite Industria S.A.",
                "uf": "SP",
                "municipio": "Campinas",
            },
            headers=cabecalho,
        )
    )
    resposta = cliente.post(
        f"/organizacao/empresas/{empresa_id}/unidades",
        data={
            "nome": "Coligada Sorocaba",
            "uf": "SP",
            "municipio": "Sorocaba",
            "participacao_societaria": "30",
        },
        headers=cabecalho,
    )
    assert resposta.status_code == 204
    unidade_id = re.search(  # type: ignore[union-attr]
        r"/organizacao/unidades/([0-9a-f-]{36})/editar",
        cliente.get(f"/organizacao/empresas/{empresa_id}").text,
    ).group(1)
    inventario_id = _id_do_redirect(
        cliente.post(
            f"/coleta/empresas/{empresa_id}/inventarios",
            data={"ano": str(ANO), "abordagem": "controle_operacional"},
            headers=cabecalho,
        )
    )
    fonte_id = _id_do_redirect(
        cliente.post(
            f"/coleta/inventarios/{inventario_id}/fontes",
            data={
                "unidade_id": unidade_id,
                "categoria": "escopo1.combustao_estacionaria",
                "descricao": "Gerador da coligada",
                "unidade_medida": "L",
                "periodicidade": "anual",
                "material": "sim",
            },
            headers=cabecalho,
        )
    )
    fonte = cliente.get(f"/coleta/fontes/{fonte_id}").text
    assert "Unidade fora do limite do inventário" in fonte
    assert "Dados aprovados da fonte" not in fonte
    inventario = cliente.get(f"/coleta/inventarios/{inventario_id}").text
    assert "Unidade fora do limite" in inventario
    assert "Cadastre as fontes de emissão" in inventario  # nada conta no progresso
