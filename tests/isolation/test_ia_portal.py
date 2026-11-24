"""IA no portal: botoes so com IA configurada, sugestao de categoria, opiniao sobre o dado,
aviso quando a IA nao responde e trilha sem conteudo. A IA e um substituto (sem rede)."""

import asyncio
import re
from typing import Any
from uuid import UUID

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.cli import criar_tenant
from app.core.config import obter_configuracao
from app.modules.ia.cliente import IAIndisponivel
from tests.isolation.conftest import Banco
from tests.isolation.portal import csrf, entrar

pytestmark = pytest.mark.integration


class IAFalsa:
    def __init__(self) -> None:
        self.fora_do_ar = False
        self.conteudos: list[str] = []

    async def gerar(self, sistema: str, conteudo: str, esquema: dict[str, Any]) -> dict[str, Any]:
        if self.fora_do_ar:
            raise IAIndisponivel("simulado")
        self.conteudos.append(conteudo)
        if "categoria" in esquema["properties"]:
            return {
                "categoria": "escopo1.combustao_movel",
                "atividade": "",
                "confianca": "media",
                "justificativa": "Veículos da própria empresa.",
            }
        return {"fora_do_padrao": True, "explicacao": "O valor é dez vezes o dos outros meses."}


async def _trilha(banco: Banco, tenant_id: UUID) -> list[asyncpg.Record]:
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        return list(
            await conexao.fetch(
                "SELECT acao, dados::text AS dados FROM audit_log WHERE tenant_id = $1"
                " AND acao = 'ia.consultada'",
                tenant_id,
            )
        )
    finally:
        await conexao.close()


def test_sugestoes_da_ia(app_cliente: TestClient, banco: Banco) -> None:
    cliente = app_cliente
    tenant_id, token = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente IA", "dona@ia.example")
    )
    entrar(cliente, f"/convite/{token}/dev", sujeito="dona-ia", email="dona@ia.example")
    cabecalho = csrf(cliente)
    cliente.post("/organizacao/grupos", data={"nome": "Grupo IA"}, headers=cabecalho)
    grupo = re.search(  # type: ignore[union-attr]
        r"/organizacao/grupos/([0-9a-f-]{36})/editar", cliente.get("/organizacao").text
    ).group(1)
    empresa = cliente.post(
        "/organizacao/empresas",
        data={"grupo_id": grupo, "cnpj": "11.222.333/0001-81", "nome": "IA S.A.", "uf": "SP",
              "municipio": "Campinas"},
        headers=cabecalho,
    ).headers["hx-redirect"].rsplit("/", 1)[-1]  # fmt: skip
    cliente.post(
        f"/organizacao/empresas/{empresa}/unidades",
        data={"nome": "Base", "uf": "SP", "municipio": "Campinas",
              "participacao_societaria": "100", "controle_operacional": "sim"},
        headers=cabecalho,
    )  # fmt: skip
    unidade = re.search(  # type: ignore[union-attr]
        r"/organizacao/unidades/([0-9a-f-]{36})/editar",
        cliente.get(f"/organizacao/empresas/{empresa}").text,
    ).group(1)
    inventario = (
        cliente.post(
            f"/coleta/empresas/{empresa}/inventarios",
            data={"ano": "2025", "abordagem": "controle_operacional"},
            headers=cabecalho,
        )
        .headers["hx-redirect"]
        .rsplit("/", 1)[-1]
    )
    url_nova = f"/coleta/inventarios/{inventario}/fontes/nova"

    # Sem IA configurada: nem botao, nem rota.
    cliente.app.state.ia = None  # type: ignore[attr-defined]
    assert "Sugerir categoria com IA" not in cliente.get(url_nova).text
    assert (
        cliente.post("/ia/fontes/sugestao", data={"descricao": "x"}, headers=cabecalho).status_code
        == 404
    )

    # IA configurada, mas o cliente ainda nao ligou (LGPD): nem botao, nem rota.
    ia = IAFalsa()
    cliente.app.state.ia = ia  # type: ignore[attr-defined]
    assert "Sugerir categoria com IA" not in cliente.get(url_nova).text
    assert (
        cliente.post("/ia/fontes/sugestao", data={"descricao": "x"}, headers=cabecalho).status_code
        == 404
    )
    usuarios = cliente.get("/usuarios").text
    assert "IA desligada para este cliente" in usuarios and "Anthropic" in usuarios
    assert cliente.post("/usuarios/ia/qualquer", headers=cabecalho).status_code == 404
    ligar = cliente.post("/usuarios/ia/ligar", headers=cabecalho)
    assert ligar.status_code == 204 and ligar.headers["hx-refresh"] == "true"
    assert "IA ligada para este cliente" in cliente.get("/usuarios").text

    # Com IA ligada: sugere entre as opcoes; nada e preenchido.
    assert "Sugerir categoria com IA" in cliente.get(url_nova).text
    sugestao = cliente.post(
        "/ia/fontes/sugestao",
        data={"descricao": "Caminhões da entrega", "unidade_medida": "L"},
        headers=cabecalho,
    )
    assert sugestao.status_code == 200
    assert "Combustão móvel" in sugestao.text and "Confiança média" in sugestao.text
    assert "Caminhões da entrega" in ia.conteudos[-1]
    vazia = cliente.post("/ia/fontes/sugestao", data={"descricao": " "}, headers=cabecalho)
    assert "Descreva a fonte primeiro" in vazia.text

    # Opiniao sobre um dado: so para quem decide; a IA nao aprova nada.
    fonte = cliente.post(
        f"/coleta/inventarios/{inventario}/fontes",
        data={"unidade_id": unidade, "categoria": "escopo1.combustao_movel",
              "descricao": "Frota", "unidade_medida": "L", "periodicidade": "mensal",
              "material": "sim"},
        headers=cabecalho,
    ).headers["hx-redirect"].rsplit("/", 1)[-1]  # fmt: skip
    for mes, valor in (("1", "1000"), ("2", "1100"), ("3", "10500")):
        cliente.post(
            f"/coleta/fontes/{fonte}/periodos/{mes}/lancamentos",
            data={"quantidade": valor},
            headers=cabecalho,
        )
    # Quem lancou nao decide: sem o botao de decisao, sem o da IA.
    assert "Pedir a opinião da IA" not in cliente.get(f"/coleta/fontes/{fonte}/periodos/3").text
    assert "/coleta/lancamentos/" not in cliente.get(f"/coleta/fontes/{fonte}/periodos/3").text
    lancamento = asyncio.run(_lancamento_de_marco(banco, UUID(fonte)))
    opiniao = cliente.post(f"/ia/lancamentos/{lancamento}/opiniao", headers=cabecalho)
    assert opiniao.status_code == 200 and "foge do padrão" in opiniao.text
    assert "Janeiro: 1000 L" in ia.conteudos[-1] and "Março: 10500 L" in ia.conteudos[-1]
    assert "Aguardando aprovação" in cliente.get(f"/coleta/fontes/{fonte}/periodos/3").text

    # IA fora do ar: aviso, sem erro.
    ia.fora_do_ar = True
    falha = cliente.post("/ia/fontes/sugestao", data={"descricao": "Caldeira"}, headers=cabecalho)
    assert falha.status_code == 200 and "A IA não respondeu agora" in falha.text

    # Trilha: cada consulta registrada, sem o texto enviado.
    registros = asyncio.run(_trilha(banco, tenant_id))
    assert len(registros) == 3
    assert all("Caminhões" not in r["dados"] and "Frota" not in r["dados"] for r in registros)

    # Desligar volta a esconder tudo, e a troca fica na trilha.
    ia.fora_do_ar = False
    cliente.post("/usuarios/ia/desligar", headers=cabecalho)
    assert "Sugerir categoria com IA" not in cliente.get(url_nova).text
    assert (
        cliente.post("/ia/fontes/sugestao", data={"descricao": "x"}, headers=cabecalho).status_code
        == 404
    )
    assert asyncio.run(_acoes(banco, tenant_id, "ia.%")) == [
        "ia.ligada",
        "ia.consultada",
        "ia.consultada",
        "ia.consultada",
        "ia.desligada",
    ]


async def _acoes(banco: Banco, tenant_id: UUID, padrao: str) -> list[str]:
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        return [
            linha["acao"]
            for linha in await conexao.fetch(
                "SELECT acao FROM audit_log WHERE tenant_id = $1 AND acao LIKE $2 ORDER BY seq",
                tenant_id,
                padrao,
            )
        ]
    finally:
        await conexao.close()


async def _lancamento_de_marco(banco: Banco, fonte: UUID) -> str:
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        return str(
            await conexao.fetchval(
                "SELECT lancamento_id FROM lancamento WHERE fonte_id = $1 AND periodo = 3", fonte
            )
        )
    finally:
        await conexao.close()
