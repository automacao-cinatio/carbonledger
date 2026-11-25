"""Pedido de relatorio no worker: fila unica por inventario, desfechos (pronto e falhou com o
motivo), pedido preso que sai da fila pelo prazo, tarefa repetida e a tela que acompanha."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import unquote
from uuid import UUID

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.cli import criar_tenant
from app.core.config import obter_configuracao
from app.core.tenancy import ContextoTenant, Escopo, conexao_de_tenant
from app.modules.relatorios import pedidos
from tests.isolation.conftest import Banco
from tests.isolation.portal import ArmazemMemoria, entrar
from tests.isolation.test_relatorios_portal import _inventario_fechado

pytestmark = pytest.mark.integration


class ProvedorQuebrado:
    """Qualquer uso da chave quebra: simula uma falha que ninguem previu."""

    def __getattr__(self, nome: str) -> Any:
        def quebrar(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("falha simulada")

        return quebrar


async def _admin(banco: Banco, sql: str, *argumentos: Any) -> list[asyncpg.Record]:
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        return list(await conexao.fetch(sql, *argumentos))
    finally:
        await conexao.close()


def _no_tenant[R](banco: Banco, tenant_id: UUID, funcao: Callable[[Any], Awaitable[R]]) -> R:
    async def rodar() -> R:
        contexto = ContextoTenant(tenant_id=tenant_id, escopo=Escopo.TENANT)
        async with conexao_de_tenant(banco.dsn_app, contexto) as conexao:
            return await funcao(conexao)

    return asyncio.run(rodar())


def _com_certificado(banco: Banco, tenant_id: UUID, empresa_id: UUID, dono: UUID) -> UUID:
    # Chave cifrada falsa: abrir o certificado falha, o que serve aos casos de erro.
    linhas = asyncio.run(
        _admin(
            banco,
            "INSERT INTO certificado_assinatura (tenant_id, certificado_id, empresa_id, titular,"
            " emissor, numero_serie, valido_de, valido_ate, impressao_sha256, chave_cifrada,"
            " enviado_por) VALUES ($1, gen_random_uuid(), $2, 'Titular', 'AC', '1', now(),"
            " now() + interval '1 year', $3, 'cl1.falsa', $4) RETURNING certificado_id",
            tenant_id,
            empresa_id,
            bytes(32),
            dono,
        )
    )
    return UUID(str(linhas[0]["certificado_id"]))


def _cenario(
    cliente: TestClient, banco: Banco, apelido: str
) -> tuple[UUID, dict[str, UUID], UUID, UUID]:
    """Cliente com a dona ja dentro (membro), inventario fechado e certificado ativo."""
    email = f"dona@{apelido}.example"
    tenant_id, token = asyncio.run(criar_tenant(obter_configuracao(), f"Cliente {apelido}", email))
    entrar(cliente, f"/convite/{token}/dev", sujeito=f"dona-{apelido}", email=email)
    ids = asyncio.run(_inventario_fechado(banco, tenant_id))
    dono = asyncio.run(
        _admin(banco, "SELECT usuario_id FROM membro WHERE tenant_id = $1", tenant_id)
    )[0]["usuario_id"]
    certificado = _com_certificado(banco, tenant_id, ids["empresa"], dono)
    return tenant_id, ids, dono, certificado


def _pedir(banco: Banco, tenant_id: UUID, ids: dict[str, UUID], dono: UUID, cert: UUID) -> UUID:
    return _no_tenant(
        banco,
        tenant_id,
        lambda c: pedidos.pedir(
            c,
            tenant_id=tenant_id,
            usuario_id=dono,
            inventario_id=ids["inventario"],
            empresa_id=ids["empresa"],
            certificado_id=cert,
        ),
    )


def _processar(banco: Banco, tenant_id: UUID, pedido_id: UUID, provedor: Any) -> str:
    return _no_tenant(
        banco,
        tenant_id,
        lambda c: pedidos.processar(
            c,
            tenant_id=tenant_id,
            pedido_id=pedido_id,
            provedor=provedor,
            armazem=ArmazemMemoria(),
            carimbadora=None,
        ),
    )


def _pedido(banco: Banco, pedido_id: UUID) -> asyncpg.Record:
    return asyncio.run(
        _admin(
            banco,
            "SELECT situacao, mensagem, relatorio_id, concluido_em FROM relatorio_pedido"
            " WHERE pedido_id = $1",
            pedido_id,
        )
    )[0]


def test_desfechos_do_pedido(app_cliente: TestClient, banco: Banco) -> None:
    tenant_id, ids, dono, certificado = _cenario(app_cliente, banco, "pedido")

    # Um so pedido na fila por inventario: o segundo clique e recusado com o que fazer.
    primeiro = _pedir(banco, tenant_id, ids, dono, certificado)
    with pytest.raises(pedidos.PedidoNaFila):
        _pedir(banco, tenant_id, ids, dono, certificado)

    # Sem a chave da plataforma: sai como falhou, com o motivo; nao fica na fila.
    assert _processar(banco, tenant_id, primeiro, None) == "falhou"
    assert _pedido(banco, primeiro)["mensagem"] == pedidos.MENSAGEM_SEM_CHAVE
    # Tarefa repetida (o Celery pode entregar duas vezes) nao mexe no desfecho.
    assert _processar(banco, tenant_id, primeiro, ProvedorQuebrado()) == "falhou"
    assert _pedido(banco, primeiro)["mensagem"] == pedidos.MENSAGEM_SEM_CHAVE

    # Falha inesperada no meio da geracao: mensagem generica, nada gravado, a transacao segue.
    segundo = _pedir(banco, tenant_id, ids, dono, certificado)
    assert _processar(banco, tenant_id, segundo, ProvedorQuebrado()) == "falhou"
    assert _pedido(banco, segundo)["mensagem"] == pedidos.MENSAGEM_FALHA_INESPERADA
    assert not asyncio.run(
        _admin(banco, "SELECT 1 FROM relatorio_inventario WHERE tenant_id = $1", tenant_id)
    )

    # Recusa da regra (certificado desativado depois do pedido): o motivo vai para o pedido.
    terceiro = _pedir(banco, tenant_id, ids, dono, certificado)
    asyncio.run(
        _admin(
            banco,
            "UPDATE certificado_assinatura SET desativado_em = now(), desativado_por = $2,"
            " chave_cifrada = NULL WHERE certificado_id = $1",
            certificado,
            dono,
        )
    )
    assert _processar(banco, tenant_id, terceiro, ProvedorQuebrado()) == "falhou"
    assert "vencido ou desativado" in _pedido(banco, terceiro)["mensagem"]

    # Pedido que o worker nao pegou no prazo sai da fila no proximo pedido.
    novo_certificado = _com_certificado(banco, tenant_id, ids["empresa"], dono)
    preso = _pedir(banco, tenant_id, ids, dono, novo_certificado)
    asyncio.run(
        _admin(
            banco,
            "UPDATE relatorio_pedido SET pedido_em = now() - interval '20 minutes'"
            " WHERE pedido_id = $1",
            preso,
        )
    )
    quarto = _pedir(banco, tenant_id, ids, dono, novo_certificado)
    assert _pedido(banco, preso)["mensagem"] == pedidos.MENSAGEM_PRAZO
    assert _pedido(banco, quarto)["situacao"] == "na_fila"
    # Se o worker pegar o pedido preso depois, nao gera nada.
    assert _processar(banco, tenant_id, preso, ProvedorQuebrado()) == "falhou"
    assert _pedido(banco, preso)["mensagem"] == pedidos.MENSAGEM_PRAZO


def test_tela_acompanha_o_pedido(app_cliente: TestClient, banco: Banco) -> None:
    cliente = app_cliente
    tenant_id, ids, dono, certificado = _cenario(cliente, banco, "fila")
    pedido = _pedir(banco, tenant_id, ids, dono, certificado)

    # Na fila: a tela mostra o andamento, consulta sozinha e nao oferece gerar de novo.
    pagina = cliente.get("/relatorios").text
    assert "Relatório sendo gerado" in pagina and "Gerar relatório assinado" not in pagina
    assert f'hx-get="/relatorios/pedidos/{pedido}/situacao"' in pagina
    assert 'hx-trigger="every 4s"' in pagina and 'aria-live="polite"' in pagina
    consulta = cliente.get(f"/relatorios/pedidos/{pedido}/situacao")
    assert consulta.status_code == 200 and "Relatório sendo gerado" in consulta.text
    assert "<html" not in consulta.text

    # Falhou: a consulta manda recarregar e a tela mostra o motivo e volta a oferecer gerar.
    _processar(banco, tenant_id, pedido, None)
    consulta = cliente.get(f"/relatorios/pedidos/{pedido}/situacao")
    assert consulta.status_code == 204 and consulta.headers["hx-refresh"] == "true"
    assert "falhou" in unquote(consulta.headers["x-aviso"])
    pagina = cliente.get("/relatorios").text
    assert "A última geração falhou" in pagina and pedidos.MENSAGEM_SEM_CHAVE in pagina
    assert "Gerar relatório assinado" in pagina

    # Pedido de outro cliente: 404, nunca 403.
    _outro, token_b = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Vizinho", "dono@vizinho.example")
    )
    entrar(cliente, f"/convite/{token_b}/dev", sujeito="dono-vizinho", email="dono@vizinho.example")
    alheio = cliente.get(
        f"/relatorios/pedidos/{pedido}/situacao", headers={"accept": "application/json"}
    )
    assert alheio.status_code == 404
