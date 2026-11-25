"""Relatorio assinado de ponta a ponta (P19): certificado da empresa, PDF do inventario
fechado, assinatura PAdES conferida pelo pyHanko, download, isolamento e desativacao.

O inventario chega fechado pela semente (superusuario, numa transacao com o evento, como a
trigger exige). O S3 e o ClamAV sao os substitutos em memoria do portal de teste; o Chromium
do Playwright e o de verdade.
"""

import asyncio
import hashlib
import re
from io import BytesIO
from typing import Any
from urllib.parse import unquote
from uuid import UUID, uuid4

import asyncpg
import pytest
from asn1crypto import x509 as asn1_x509
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.validation import async_validate_pdf_signature
from pyhanko_certvalidator import ValidationContext

from app.cli import criar_tenant
from app.core.config import obter_configuracao
from tests.certificados_teste import SENHA, gerar_pfx
from tests.isolation.conftest import Banco
from tests.isolation.portal import csrf, entrar

pytestmark = pytest.mark.integration

CNPJ = "11222333000181"


def _aviso(resposta: Any) -> str:
    assert resposta.status_code == 204, resposta.text
    return unquote(resposta.headers["x-aviso"])


def _campos(resposta: Any) -> set[str]:
    assert resposta.status_code == 422, resposta.text
    return {e["campo"] for e in resposta.json()["erros"]}


async def _inventario_fechado(banco: Banco, tenant_id: UUID) -> dict[str, UUID]:
    ids = {n: uuid4() for n in ("grupo", "empresa", "unidade", "inventario", "fonte", "calculo")}
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        dono = await conexao.fetchval(
            "SELECT usuario_id FROM membro WHERE tenant_id = $1", tenant_id
        )
        async with conexao.transaction():
            await conexao.execute(
                "INSERT INTO grupo (tenant_id, grupo_id, nome) VALUES ($1, $2, 'Grupo R')",
                tenant_id,
                ids["grupo"],
            )
            await conexao.execute(
                "INSERT INTO empresa (tenant_id, empresa_id, grupo_id, nome, cnpj)"
                " VALUES ($1, $2, $3, 'Relatada S.A.', $4)",
                tenant_id,
                ids["empresa"],
                ids["grupo"],
                CNPJ,
            )
            await conexao.execute(
                "INSERT INTO unidade (tenant_id, unidade_id, empresa_id, nome, uf, municipio,"
                " controle_operacional, controle_financeiro) VALUES ($1, $2, $3, 'Fabrica R',"
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
                " VALUES ($1, $2, $3, $4, $5, 'escopo1.combustao_estacionaria', 'Caldeira R',"
                " 'L', 'anual', 'x')",
                tenant_id,
                ids["fonte"],
                ids["empresa"],
                ids["inventario"],
                ids["unidade"],
            )
            await conexao.execute(
                "INSERT INTO calculo (tenant_id, calculo_id, empresa_id, inventario_id, metodo,"
                " metodo_versao, gwp_conjunto, conhecido_em, entrada, hash_entrada,"
                " hash_resultado, escopo1_tco2e, escopo2_tco2e, biogenico_tco2, linhas,"
                " pendencias) VALUES ($1, $2, $3, $4, 'fator_direto', '1.2.0', 'AR5_100', now(),"
                " '{}', $5, $5, 1234.5678, 10, 0, 0, 0)",
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
    finally:
        await conexao.close()
    return ids


async def _consultar(banco: Banco, sql: str, *argumentos: Any) -> list[asyncpg.Record]:
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        return list(await conexao.fetch(sql, *argumentos))
    finally:
        await conexao.close()


async def _validar(pdf: bytes, raiz: bytes) -> Any:
    leitor = PdfFileReader(BytesIO(pdf))
    assert len(leitor.embedded_signatures) == 1
    contexto = ValidationContext(trust_roots=[asn1_x509.Certificate.load(raiz)])
    return await async_validate_pdf_signature(
        leitor.embedded_signatures[0], signer_validation_context=contexto
    )


def _enviar(cliente: TestClient, empresa: UUID, pfx: bytes, senha: str, nome: str) -> Any:
    return cliente.post(
        f"/relatorios/empresas/{empresa}/certificado",
        files={"arquivo": (nome, pfx, "application/x-pkcs12")},
        data={"senha": senha},
        headers=csrf(cliente),
    )


def test_roteiro_do_relatorio_assinado(
    app_cliente: TestClient, banco: Banco, emails: list[dict[str, str]]
) -> None:
    cliente = app_cliente
    armazem = cliente.app.state.armazem  # type: ignore[attr-defined]
    tenant_id, token = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Relatado", "dona@relatado.example")
    )
    entrar(cliente, f"/convite/{token}/dev", sujeito="dona-relatado", email="dona@relatado.example")
    ids = asyncio.run(_inventario_fechado(banco, tenant_id))
    empresa = ids["empresa"]

    # Sem certificado: a tela explica e nao oferece gerar.
    pagina = cliente.get("/relatorios").text
    assert 'href="/relatorios"' in pagina and "Falta o certificado" in pagina
    assert "Gerar relatório assinado" not in pagina
    assert (
        cliente.post(
            f"/relatorios/inventarios/{ids['inventario']}/gerar", headers=csrf(cliente)
        ).status_code
        == 422
    )

    # Recusas: senha errada, CNPJ de outra empresa, extensao errada.
    pfx, certificado = gerar_pfx(cnpj=CNPJ)
    assert _campos(_enviar(cliente, empresa, pfx, "errada", "empresa.pfx")) == {"senha"}
    outro, _ = gerar_pfx(cnpj="99888777000166")
    assert _campos(_enviar(cliente, empresa, outro, SENHA, "empresa.pfx")) == {"arquivo"}
    assert _campos(_enviar(cliente, empresa, pfx, SENHA, "empresa.txt")) == {"arquivo"}

    # Certificado certo: guardado cifrado, sem a senha.
    assert "senha não foi guardada" in _aviso(_enviar(cliente, empresa, pfx, SENHA, "e.pfx"))
    pagina = cliente.get("/relatorios").text
    assert "EMPRESA TESTE SA" in pagina and "Vigente" in pagina
    assert "11.222.333/0001-81" in pagina and "Gerar relatório assinado" in pagina
    guardado = asyncio.run(
        _consultar(banco, "SELECT chave_cifrada, cnpj FROM certificado_assinatura"
                   " WHERE tenant_id = $1", tenant_id)
    )  # fmt: skip
    assert len(guardado) == 1 and guardado[0]["cnpj"] == CNPJ
    assert guardado[0]["chave_cifrada"].startswith("cl1.")
    assert SENHA not in guardado[0]["chave_cifrada"]

    # Gera: o pedido vai para o worker (aqui, na hora); PDF assinado, guardado e registrado.
    gerado = cliente.post(
        f"/relatorios/inventarios/{ids['inventario']}/gerar", headers=csrf(cliente)
    )
    assert _aviso(gerado).startswith("Pedido na fila.")
    pedido = asyncio.run(
        _consultar(banco, "SELECT pedido_id, situacao, relatorio_id, mensagem FROM"
                   " relatorio_pedido WHERE tenant_id = $1", tenant_id)
    )  # fmt: skip
    assert len(pedido) == 1 and pedido[0]["situacao"] == "pronto", pedido
    assert pedido[0]["relatorio_id"] is not None and pedido[0]["mensagem"] is None
    # A consulta da tela, com o pedido pronto, manda recarregar.
    consulta = cliente.get(f"/relatorios/pedidos/{pedido[0]['pedido_id']}/situacao")
    assert consulta.status_code == 204 and consulta.headers["hx-refresh"] == "true"
    assert _aviso(consulta) == "Relatório gerado e assinado."
    pagina = cliente.get("/relatorios").text
    assert "Fechamento atual" in pagina
    link = re.search(r'href="(/relatorios/[0-9a-f-]{36}/arquivo\.pdf)"', pagina).group(1)  # type: ignore[union-attr]
    baixado = cliente.get(link)
    assert baixado.status_code == 200 and baixado.headers["content-type"] == "application/pdf"
    assert "inventario-2025-fechamento-v1.pdf" in baixado.headers["content-disposition"]
    assert "no-store" in baixado.headers["cache-control"]
    raiz = certificado.public_bytes(serialization.Encoding.DER)
    estado = asyncio.run(_validar(baixado.content, raiz))
    assert estado.intact and estado.valid and estado.coverage.name == "ENTIRE_FILE"
    registro = asyncio.run(
        _consultar(banco, "SELECT sha256, hash_fechamento, chave_objeto FROM"
                   " relatorio_inventario WHERE tenant_id = $1", tenant_id)
    )  # fmt: skip
    assert bytes(registro[0]["sha256"]) == hashlib.sha256(baixado.content).digest()
    assert bytes(registro[0]["hash_fechamento"]) == bytes(range(32))
    assert registro[0]["chave_objeto"] in armazem.objetos

    # O verificador do inventario ve e baixa o relatorio pelo painel dele.
    concedido = cliente.post(
        "/verificacao/concessoes",
        data={"inventario_id": str(ids["inventario"]), "email": "auditor@verifica.example",
              "prazo_dias": ""},
        headers=csrf(cliente),
    )  # fmt: skip
    assert concedido.status_code == 204, concedido.text
    convite = re.search(r"http://testserver(/verificacao/convite/\S+/dev)", emails[-1]["corpo"])
    assert convite is not None
    entrar(cliente, convite.group(1), sujeito="auditor-r", email="auditor@verifica.example")
    painel = cliente.get("/verificacao/painel").text
    assert "Relatório assinado pela empresa" in painel and "Fechamento atual" in painel
    assert hashlib.sha256(baixado.content).hexdigest() in painel
    do_verificador = re.search(  # type: ignore[union-attr]
        r'href="(/verificacao/relatorios/[0-9a-f-]{36}/arquivo\.pdf)"', painel
    ).group(1)
    pelo_verificador = cliente.get(do_verificador)
    assert pelo_verificador.status_code == 200 and pelo_verificador.content == baixado.content
    assert cliente.get(link, headers={"accept": "application/json"}).status_code == 404
    entrar(cliente, "/entrar/dev", sujeito="dona-relatado", email="dona@relatado.example")

    # Outro cliente nao baixa (404, nunca 403).
    _outro_tenant, token_b = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Curioso", "dono@curioso.example")
    )
    entrar(cliente, f"/convite/{token_b}/dev", sujeito="dono-curioso", email="dono@curioso.example")
    assert cliente.get(link, headers={"accept": "application/json"}).status_code == 404

    # Desativar apaga a chave; o relatorio continua; gerar de novo exige certificado.
    entrar(cliente, "/entrar/dev", sujeito="dona-relatado", email="dona@relatado.example")
    certificado_id = re.search(  # type: ignore[union-attr]
        r"/relatorios/certificados/([0-9a-f-]{36})/desativar", cliente.get("/relatorios").text
    ).group(1)
    assert "chave foi apagada" in _aviso(
        cliente.post(f"/relatorios/certificados/{certificado_id}/desativar", headers=csrf(cliente))
    )
    assert (
        cliente.post(
            f"/relatorios/certificados/{certificado_id}/desativar", headers=csrf(cliente)
        ).status_code
        == 422
    )
    pagina = cliente.get("/relatorios").text
    assert "Nenhum certificado ativo" in pagina and link in pagina
    assert cliente.get(link).status_code == 200
    apagada = asyncio.run(
        _consultar(banco, "SELECT chave_cifrada FROM certificado_assinatura"
                   " WHERE certificado_id = $1", UUID(certificado_id))
    )  # fmt: skip
    assert apagada[0]["chave_cifrada"] is None

    acoes = [
        r["acao"]
        for r in asyncio.run(
            _consultar(banco, "SELECT acao FROM audit_log WHERE tenant_id = $1 AND (acao LIKE"
                       " 'certificado.%' OR acao LIKE 'relatorio.%') ORDER BY seq", tenant_id)
        )
    ]  # fmt: skip
    assert acoes == [
        "certificado.cadastrado",
        "relatorio.pedido",
        "relatorio.gerado",
        "certificado.desativado",
    ]


def test_certificado_so_desativa_no_banco(banco: Banco) -> None:
    """Nem o app consegue trocar dados do certificado ou reativar: so desativar, uma vez."""

    async def roteiro() -> None:
        conexao = await asyncpg.connect(banco.dsn_admin)
        try:
            linha = await conexao.fetchrow(
                "SELECT certificado_id FROM certificado_assinatura LIMIT 1"
            )
            assert linha is not None
            for comando in (
                "UPDATE certificado_assinatura SET titular = 'Outro' WHERE certificado_id = $1",
                "UPDATE certificado_assinatura SET desativado_em = now(), desativado_por ="
                " gen_random_uuid() WHERE certificado_id = $1",
                "DELETE FROM certificado_assinatura WHERE certificado_id = $1",
            ):
                with pytest.raises(asyncpg.PostgresError):
                    async with conexao.transaction():
                        await conexao.execute(comando, linha["certificado_id"])
            for privilegio, esperado in (
                ("UPDATE", False),
                ("DELETE", False),
                ("TRUNCATE", False),
            ):
                tem = await conexao.fetchval(
                    "SELECT has_table_privilege('app_user', 'certificado_assinatura', $1)",
                    privilegio,
                )
                assert tem is esperado
            assert await conexao.fetchval(
                "SELECT has_column_privilege('app_user', 'certificado_assinatura',"
                " 'chave_cifrada', 'UPDATE')"
            )
            assert not await conexao.fetchval(
                "SELECT has_column_privilege('app_user', 'certificado_assinatura',"
                " 'titular', 'UPDATE')"
            )
        finally:
            await conexao.close()

    asyncio.run(roteiro())
