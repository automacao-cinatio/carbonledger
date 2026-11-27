"""Verificacao de integridade pedida na tela, no worker: fila unica por cliente, desfechos
(concluido com a verificacao e falhou com o motivo), tarefa repetida, pedido preso e a tela que
acompanha."""

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
from app.modules.integridade import pedidos
from tests.isolation.conftest import Banco
from tests.isolation.portal import ArmazemMemoria, entrar

pytestmark = pytest.mark.integration


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


def _cenario(cliente: TestClient, banco: Banco, apelido: str) -> tuple[UUID, UUID]:
    """Cliente novo com a dona ja dentro (administradora)."""
    email = f"dona@{apelido}.example"
    tenant_id, token = asyncio.run(criar_tenant(obter_configuracao(), f"Cliente {apelido}", email))
    entrar(cliente, f"/convite/{token}/dev", sujeito=f"dona-{apelido}", email=email)
    dono = asyncio.run(
        _admin(banco, "SELECT usuario_id FROM membro WHERE tenant_id = $1", tenant_id)
    )[0]["usuario_id"]
    return tenant_id, dono


def _pedir(banco: Banco, tenant_id: UUID, dono: UUID) -> UUID:
    return _no_tenant(
        banco, tenant_id, lambda c: pedidos.pedir(c, tenant_id=tenant_id, usuario_id=dono)
    )


def _processar(banco: Banco, tenant_id: UUID, pedido_id: UUID) -> str:
    return _no_tenant(
        banco,
        tenant_id,
        lambda c: pedidos.processar(
            c, tenant_id=tenant_id, pedido_id=pedido_id, armazem=ArmazemMemoria()
        ),
    )


def _pedido(banco: Banco, pedido_id: UUID) -> asyncpg.Record:
    return asyncio.run(
        _admin(
            banco,
            "SELECT situacao, mensagem, verificacao_id, concluido_em FROM integridade_pedido"
            " WHERE pedido_id = $1",
            pedido_id,
        )
    )[0]


def _verificacoes(banco: Banco, tenant_id: UUID) -> int:
    linhas = asyncio.run(
        _admin(
            banco,
            "SELECT count(*) AS total FROM integridade_verificacao WHERE tenant_id = $1",
            tenant_id,
        )
    )
    return int(linhas[0]["total"])


def _envelhecer(banco: Banco, pedido_id: UUID) -> None:
    asyncio.run(
        _admin(
            banco,
            "UPDATE integridade_pedido SET pedido_em = now() - interval '2 hours'"
            " WHERE pedido_id = $1",
            pedido_id,
        )
    )


def test_desfechos_do_pedido(
    app_cliente: TestClient, banco: Banco, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, dono = _cenario(app_cliente, banco, "integra")

    # Um so pedido na fila por cliente: o segundo clique e recusado com o que fazer.
    primeiro = _pedir(banco, tenant_id, dono)
    with pytest.raises(pedidos.PedidoNaFila):
        _pedir(banco, tenant_id, dono)

    # Concluido: a verificacao fica gravada e amarrada ao pedido, pela mesma funcao da diaria.
    assert _processar(banco, tenant_id, primeiro) == "concluido"
    concluido = _pedido(banco, primeiro)
    assert concluido["verificacao_id"] is not None and concluido["mensagem"] is None
    assert _verificacoes(banco, tenant_id) == 1
    # Tarefa repetida (o Celery pode entregar duas vezes) nao verifica de novo.
    assert _processar(banco, tenant_id, primeiro) == "concluido"
    assert _verificacoes(banco, tenant_id) == 1

    # Falha inesperada no meio: mensagem generica, nenhuma verificacao gravada, o pedido sai
    # da fila (a transacao do worker segue e grava o desfecho).
    def quebrar(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("falha simulada")

    segundo = _pedir(banco, tenant_id, dono)
    with monkeypatch.context() as trocas:
        trocas.setattr(pedidos.servico, "verificar", quebrar)
        assert _processar(banco, tenant_id, segundo) == "falhou"
    assert _pedido(banco, segundo)["mensagem"] == pedidos.MENSAGEM_FALHA_INESPERADA
    assert _verificacoes(banco, tenant_id) == 1

    # Pedido que o worker nao pegou no prazo sai da fila no proximo pedido.
    preso = _pedir(banco, tenant_id, dono)
    _envelhecer(banco, preso)
    terceiro = _pedir(banco, tenant_id, dono)
    assert _pedido(banco, preso)["mensagem"] == pedidos.MENSAGEM_PRAZO
    assert _pedido(banco, terceiro)["situacao"] == "na_fila"
    # Se o worker pegar o pedido preso depois, nao verifica.
    assert _processar(banco, tenant_id, preso) == "falhou"
    assert _verificacoes(banco, tenant_id) == 1

    # Preso que o worker pega antes de qualquer novo pedido tambem sai como falhou, sem verificar.
    _envelhecer(banco, terceiro)
    assert _processar(banco, tenant_id, terceiro) == "falhou"
    assert _pedido(banco, terceiro)["mensagem"] == pedidos.MENSAGEM_PRAZO
    assert _verificacoes(banco, tenant_id) == 1

    # Tudo na trilha: cada pedido e cada verificacao.
    acoes = [
        linha["acao"]
        for linha in asyncio.run(
            _admin(
                banco,
                "SELECT acao FROM audit_log WHERE tenant_id = $1 AND acao LIKE 'integridade.%'",
                tenant_id,
            )
        )
    ]
    assert acoes.count("integridade.pedida") == 4 and acoes.count("integridade.verificada") == 1


def test_tela_acompanha_o_pedido(app_cliente: TestClient, banco: Banco) -> None:
    cliente = app_cliente
    tenant_id, dono = _cenario(cliente, banco, "acompanha")
    pedido = _pedir(banco, tenant_id, dono)

    # Na fila: a tela mostra o andamento, consulta sozinha e nao oferece verificar de novo.
    pagina = cliente.get("/integridade").text
    assert "Verificação em andamento" in pagina and "Verificar agora" not in pagina
    assert f'hx-get="/integridade/pedidos/{pedido}/situacao"' in pagina
    assert 'hx-trigger="every 5s"' in pagina and 'aria-live="polite"' in pagina
    consulta = cliente.get(f"/integridade/pedidos/{pedido}/situacao")
    assert consulta.status_code == 200 and "Verificação em andamento" in consulta.text
    assert "<html" not in consulta.text

    # Concluido: a consulta manda recarregar e a tela mostra o resultado e o botao.
    assert _processar(banco, tenant_id, pedido) == "concluido"
    consulta = cliente.get(f"/integridade/pedidos/{pedido}/situacao")
    assert consulta.status_code == 204 and consulta.headers["hx-refresh"] == "true"
    assert "concluída" in unquote(consulta.headers["x-aviso"])
    pagina = cliente.get("/integridade").text
    assert "Tudo confere" in pagina and "Verificar agora" in pagina

    # Preso alem do prazo: a tela mostra o motivo e devolve o botao, sem escrever na leitura.
    preso = _pedir(banco, tenant_id, dono)
    _envelhecer(banco, preso)
    pagina = cliente.get("/integridade").text
    assert "Verificação em andamento" not in pagina and pedidos.MENSAGEM_PRAZO in pagina
    assert "Verificar agora" in pagina
    assert _pedido(banco, preso)["situacao"] == "na_fila"

    # Pedido de outro cliente: 404, nunca 403.
    _outro, token_b = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Vizinho Integra", "dono@vizinho-int.example")
    )
    entrar(
        cliente,
        f"/convite/{token_b}/dev",
        sujeito="dono-vizinho-int",
        email="dono@vizinho-int.example",
    )
    alheio = cliente.get(
        f"/integridade/pedidos/{pedido}/situacao", headers={"accept": "application/json"}
    )
    assert alheio.status_code == 404


def test_botao_pede_e_o_worker_conclui(app_cliente: TestClient, banco: Banco) -> None:
    """Pelo botao: o pedido e gravado, a tarefa roda depois do commit e o resultado aparece."""
    cliente = app_cliente
    tenant_id, _dono = _cenario(cliente, banco, "botao")
    csrf = cliente.get("/sessao").json()["csrf"]
    resposta = cliente.post("/integridade/verificar", headers={"X-CSRF-Token": csrf})
    assert resposta.status_code == 204
    assert unquote(resposta.headers["x-aviso"]).startswith("Verificação pedida")
    situacoes = asyncio.run(
        _admin(banco, "SELECT situacao FROM integridade_pedido WHERE tenant_id = $1", tenant_id)
    )
    assert [linha["situacao"] for linha in situacoes] == ["concluido"]
    assert "Tudo confere" in cliente.get("/integridade").text
