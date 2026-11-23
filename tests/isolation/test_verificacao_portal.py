"""Verificador de ponta a ponta: concessao, link por e-mail, painel so de leitura e revogacao.

O inventario chega fechado pela semente do teste (superusuario, numa transacao com o evento,
como a trigger exige). O S3 e o ClamAV sao os substitutos em memoria do portal de teste.
"""

import asyncio
import hashlib
import re
from typing import Any
from urllib.parse import unquote
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.cli import criar_tenant
from app.core.config import obter_configuracao
from tests.isolation.conftest import Banco
from tests.isolation.portal import csrf, entrar

pytestmark = pytest.mark.integration

PDF = b"%PDF-1.7 nota fiscal de teste"


async def _inventario_fechado(banco: Banco, tenant_id: UUID, dono: UUID) -> dict[str, Any]:
    nomes = (
        "grupo", "empresa", "unidade", "inventario", "fonte",
        "lancamento", "calculo", "evidencia", "outra_fonte",
    )  # fmt: skip
    ids = {nome: uuid4() for nome in nomes}
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        async with conexao.transaction():
            await conexao.execute(
                "INSERT INTO grupo (tenant_id, grupo_id, nome) VALUES ($1, $2, 'Grupo V')",
                tenant_id,
                ids["grupo"],
            )
            await conexao.execute(
                "INSERT INTO empresa (tenant_id, empresa_id, grupo_id, nome) VALUES ($1, $2, $3,"
                " 'Empresa Verificada')",
                tenant_id,
                ids["empresa"],
                ids["grupo"],
            )
            await conexao.execute(
                "INSERT INTO unidade (tenant_id, unidade_id, empresa_id, nome, uf, municipio,"
                " controle_operacional, controle_financeiro) VALUES ($1, $2, $3, 'Fabrica V',"
                " 'SP', 'Campinas', true, true)",
                tenant_id,
                ids["unidade"],
                ids["empresa"],
            )
            await conexao.execute(
                "INSERT INTO inventario (tenant_id, inventario_id, empresa_id, ano, abordagem)"
                " VALUES ($1, $2, $3, 2025, 'controle_operacional')",
                tenant_id,
                ids["inventario"],
                ids["empresa"],
            )
            await conexao.execute(
                "INSERT INTO fonte_emissao (tenant_id, fonte_id, empresa_id, inventario_id,"
                " unidade_id, categoria, descricao, unidade_medida, periodicidade, atividade)"
                " VALUES ($1, $2, $3, $4, $5, 'escopo1.combustao_estacionaria', 'Caldeira V',"
                " 'L', 'anual', 'x')",
                tenant_id,
                ids["fonte"],
                ids["empresa"],
                ids["inventario"],
                ids["unidade"],
            )
            await conexao.execute(
                "INSERT INTO lancamento (tenant_id, lancamento_id, empresa_id, fonte_id, periodo,"
                " versao, quantidade, unidade, origem) VALUES ($1, $2, $3, $4, 0, 1, 500, 'L',"
                " 'manual')",
                tenant_id,
                ids["lancamento"],
                ids["empresa"],
                ids["fonte"],
            )
            await conexao.execute(
                "INSERT INTO lancamento_decisao (tenant_id, lancamento_id, empresa_id, decisao,"
                " decidido_por) VALUES ($1, $2, $3, 'aprovado', $4)",
                tenant_id,
                ids["lancamento"],
                ids["empresa"],
                dono,
            )
            await conexao.execute(
                "INSERT INTO evidencia (tenant_id, evidencia_id, empresa_id, fonte_id, periodo,"
                " nome_arquivo, tipo_conteudo, tamanho, sha256, chave_objeto) VALUES ($1, $2, $3,"
                " $4, 0, 'nota.pdf', 'application/pdf', $5, $6, $7)",
                tenant_id,
                ids["evidencia"],
                ids["empresa"],
                ids["fonte"],
                len(PDF),
                hashlib.sha256(PDF).digest(),
                f"evidencias/{ids['evidencia']}",
            )
            await conexao.execute(
                "INSERT INTO calculo (tenant_id, calculo_id, empresa_id, inventario_id, metodo,"
                " metodo_versao, gwp_conjunto, conhecido_em, entrada, hash_entrada,"
                " hash_resultado, escopo1_tco2e, biogenico_tco2, linhas, pendencias)"
                " VALUES ($1, $2, $3, $4, 'fator_direto', '1.1.0', 'AR5_100', now(), '{}', $5,"
                " $5, 1.5, 0, 0, 0)",
                tenant_id,
                ids["calculo"],
                ids["empresa"],
                ids["inventario"],
                bytes(32),
            )
            await conexao.execute(
                "INSERT INTO inventario_evento (tenant_id, evento_id, empresa_id, inventario_id,"
                " tipo, versao, calculo_id, hash_fechamento, registrado_por) VALUES ($1,"
                " gen_random_uuid(), $2, $3, 'fechamento', 1, $4, $5, $6)",
                tenant_id,
                ids["empresa"],
                ids["inventario"],
                ids["calculo"],
                bytes(range(32)),
                dono,
            )
            await conexao.execute(
                "UPDATE inventario SET estado = 'fechado' WHERE inventario_id = $1",
                ids["inventario"],
            )
            # Outra fonte, de um inventario em coleta da mesma empresa: o verificador nao ve.
            outro = uuid4()
            await conexao.execute(
                "INSERT INTO inventario (tenant_id, inventario_id, empresa_id, ano, abordagem)"
                " VALUES ($1, $2, $3, 2024, 'controle_operacional')",
                tenant_id,
                outro,
                ids["empresa"],
            )
            await conexao.execute(
                "INSERT INTO fonte_emissao (tenant_id, fonte_id, empresa_id, inventario_id,"
                " unidade_id, categoria, descricao, unidade_medida, periodicidade) VALUES ($1,"
                " $2, $3, $4, $5, 'escopo1.fugitivas', 'Outra', 'kg', 'anual')",
                tenant_id,
                ids["outra_fonte"],
                ids["empresa"],
                outro,
                ids["unidade"],
            )
    finally:
        await conexao.close()
    return ids


async def _dono(banco: Banco, tenant_id: UUID) -> UUID:
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        return UUID(
            str(
                await conexao.fetchval(
                    "SELECT usuario_id FROM membro WHERE tenant_id = $1", tenant_id
                )
            )
        )
    finally:
        await conexao.close()


def _link(corpo: str) -> str:
    achado = re.search(r"http://testserver(/verificacao/convite/\S+/dev)", corpo)
    assert achado, corpo
    return achado.group(1)


def test_roteiro_do_verificador(
    app_cliente: TestClient, banco: Banco, emails: list[dict[str, str]]
) -> None:
    cliente = app_cliente
    armazem = cliente.app.state.armazem  # type: ignore[attr-defined]
    tenant_id, token = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Verificado", "dona@verificado.example")
    )
    entrar(
        cliente, f"/convite/{token}/dev", sujeito="dona-verificado", email="dona@verificado.example"
    )
    dono = asyncio.run(_dono(banco, tenant_id))
    ids = asyncio.run(_inventario_fechado(banco, tenant_id, dono))
    armazem.objetos[f"evidencias/{ids['evidencia']}"] = (PDF, "application/pdf")
    cabecalho = csrf(cliente)

    # 1. O administrador concede: e-mail e prazo validados; so inventario fechado.
    pagina = cliente.get("/verificacao")
    assert pagina.status_code == 200 and "Dar acesso ao verificador" in pagina.text
    ruim = cliente.post(
        "/verificacao/concessoes",
        data={"inventario_id": str(ids["inventario"]), "email": "x", "prazo_dias": "400"},
        headers=cabecalho,
    )
    assert ruim.status_code == 422
    assert {e["campo"] for e in ruim.json()["erros"]} == {"email", "prazo_dias"}
    concedido = cliente.post(
        "/verificacao/concessoes",
        data={
            "inventario_id": str(ids["inventario"]),
            "email": "auditora@verifica.example",
            "prazo_dias": "",
        },
        headers=cabecalho,
    )
    assert concedido.status_code == 204, concedido.text
    assert unquote(concedido.headers["x-aviso"]).startswith("Acesso concedido")
    link = _link(emails[-1]["corpo"])
    assert "90 dias" in emails[-1]["corpo"]
    assert "Aguardando aceite" in cliente.get("/verificacao").text

    # 2. O verificador aceita pelo link e cai no painel, so de leitura.
    volta = entrar(cliente, link, sujeito="auditora", email="auditora@verifica.example")
    assert volta.status_code == 303 and volta.headers["location"] == "/verificacao/painel"
    painel = cliente.get("/verificacao/painel")
    assert painel.status_code == 200
    assert "Empresa Verificada" in painel.text and "Caldeira V" in painel.text
    assert "Outra" not in painel.text
    assert bytes(range(32)).hex() in painel.text  # hash do fechamento
    fonte = cliente.get(f"/verificacao/fontes/{ids['fonte']}")
    assert fonte.status_code == 200 and hashlib.sha256(PDF).hexdigest() in fonte.text
    baixado = cliente.get(f"/verificacao/evidencias/{ids['evidencia']}/arquivo")
    assert baixado.status_code == 200 and baixado.content == PDF
    assert cliente.get(f"/verificacao/calculos/{ids['calculo']}/memoria.csv").status_code == 200

    # Nada fora do painel: telas do cliente, a outra fonte e escrita respondem 404.
    for caminho in (
        "/organizacao",
        "/coleta",
        f"/coleta/fontes/{ids['fonte']}",
        "/resultados",
        "/verificacao",
        f"/verificacao/fontes/{ids['outra_fonte']}",
    ):
        assert cliente.get(caminho, headers={"accept": "application/json"}).status_code == 404, (
            caminho
        )
    cab_verif = csrf(cliente)
    assert (
        cliente.post(
            f"/coleta/fontes/{ids['fonte']}/periodos/0/lancamentos",
            data={"quantidade": "1"},
            headers=cab_verif,
        ).status_code
        == 404
    )

    # O link e de uso unico.
    assert (
        entrar(cliente, link, sujeito="auditora", email="auditora@verifica.example").status_code
        == 404
    )

    # 3. Entrando de novo pelo provedor, a verificacao aparece na escolha de cliente.
    entrar(cliente, "/entrar/dev", sujeito="auditora", email="auditora@verifica.example")
    clientes = cliente.get("/clientes")
    assert "Empresa Verificada, inventário 2025" in clientes.text
    concessao_id = re.search(  # type: ignore[union-attr]
        r"/verificacao/abrir/[0-9a-f-]{36}/([0-9a-f-]{36})", clientes.text
    ).group(1)
    aberto = cliente.post(f"/verificacao/abrir/{tenant_id}/{concessao_id}", headers=csrf(cliente))
    assert aberto.status_code == 204 and aberto.headers["hx-redirect"] == "/verificacao/painel"
    assert cliente.get("/verificacao/painel").status_code == 200

    # 4. O administrador revoga: o verificador perde o acesso na hora.
    entrar(cliente, "/entrar/dev", sujeito="dona-verificado", email="dona@verificado.example")
    cabecalho = csrf(cliente)
    revogado = cliente.post(f"/verificacao/concessoes/{concessao_id}/revogar", headers=cabecalho)
    assert revogado.status_code == 204
    assert "Revogada" in cliente.get("/verificacao").text
    entrar(cliente, "/entrar/dev", sujeito="auditora", email="auditora@verifica.example")
    assert "Empresa Verificada" not in cliente.get("/clientes").text
    assert (
        cliente.post(
            f"/verificacao/abrir/{tenant_id}/{concessao_id}", headers=csrf(cliente)
        ).status_code
        == 404
    )
