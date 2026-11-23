"""Campanhas no banco: lembrete devido sai uma vez so, vencimento, e encerramento sozinho.

Semente do tenant A: fonte mensal com janeiro aprovado, sem responsavel nem colaborador com
empresa atribuida a A1 alem do responsavel A1 (que recebe os avisos).
"""

from collections.abc import AsyncIterator
from datetime import date

import asyncpg
import pytest

from app.core.erros import ErrosDeValidacao
from app.core.tenancy import ContextoTenant, Escopo, aplicar_contexto
from app.modules.coleta import campanhas
from tests.isolation.conftest import Dados

pytestmark = pytest.mark.integration

PRAZO = date(2026, 3, 20)


@pytest.fixture
async def tenant_a(
    conexao_app: asyncpg.Connection, dados: Dados
) -> AsyncIterator[asyncpg.Connection]:
    transacao = conexao_app.transaction()
    await transacao.start()
    try:
        await aplicar_contexto(
            conexao_app, ContextoTenant(tenant_id=dados.tenant_a, escopo=Escopo.TENANT)
        )
        # A campanha da semente fica fora do caminho; abre-se uma nova com prazo conhecido.
        await conexao_app.execute(
            "UPDATE campanha SET encerrada_em = now(), motivo_encerramento = 'manual'"
            " WHERE encerrada_em IS NULL"
        )
        yield conexao_app
    finally:
        await transacao.rollback()


class Caixa:
    def __init__(self) -> None:
        self.mensagens: list[tuple[str, str, str]] = []

    async def __call__(self, para: str, assunto: str, corpo: str) -> None:
        self.mensagens.append((para, assunto, corpo))


async def test_abrir_avisa_a_equipe_com_o_que_falta(
    tenant_a: asyncpg.Connection, dados: Dados
) -> None:
    caixa = Caixa()
    _id, avisados = await campanhas.abrir(
        tenant_a,
        tenant_id=dados.tenant_a,
        usuario_id=dados.admin_a,
        inventario_id=dados.inventario_a1,
        formulario={"prazo": PRAZO.isoformat(), "mensagem": "Contamos com vocês."},
        hoje=date(2026, 3, 1),
        enviar=caixa,
    )
    assert avisados == 1
    para, assunto, corpo = caixa.mensagens[0]
    assert para == "resp@a.example"
    assert "prazo 20/03/2026" in assunto
    assert "Caldeira: Fevereiro (sem dado)" in corpo and "Janeiro" not in corpo
    assert "Contamos com vocês." in corpo
    with pytest.raises(ErrosDeValidacao, match="Já há uma campanha aberta"):
        await campanhas.abrir(
            tenant_a,
            tenant_id=dados.tenant_a,
            usuario_id=dados.admin_a,
            inventario_id=dados.inventario_a1,
            formulario={"prazo": PRAZO.isoformat()},
            hoje=date(2026, 3, 1),
            enviar=caixa,
        )


async def test_lembrete_sai_uma_vez_e_vencida_depois(
    tenant_a: asyncpg.Connection, dados: Dados
) -> None:
    caixa = Caixa()
    await campanhas.abrir(
        tenant_a,
        tenant_id=dados.tenant_a,
        usuario_id=dados.admin_a,
        inventario_id=dados.inventario_a1,
        formulario={"prazo": PRAZO.isoformat()},
        hoje=date(2026, 3, 1),
        enviar=caixa,
    )
    assert await campanhas.lembrar(tenant_a, dados.tenant_a, date(2026, 3, 1), caixa) == 0
    assert await campanhas.lembrar(tenant_a, dados.tenant_a, date(2026, 3, 17), caixa) == 1
    assert "Faltam 3 dias" in caixa.mensagens[-1][1]
    assert await campanhas.lembrar(tenant_a, dados.tenant_a, date(2026, 3, 18), caixa) == 0
    assert await campanhas.lembrar(tenant_a, dados.tenant_a, date(2026, 3, 21), caixa) == 1
    assert "venceu" in caixa.mensagens[-1][1]
    tipos = [
        linha["tipo"]
        for linha in await tenant_a.fetch(
            "SELECT e.tipo FROM campanha_envio e JOIN campanha c"
            " ON c.tenant_id = e.tenant_id AND c.campanha_id = e.campanha_id"
            " WHERE c.encerrada_em IS NULL ORDER BY e.enviado_em, e.tipo"
        )
    ]
    assert sorted(tipos) == ["abertura", "lembrete_3", "vencida"]


async def test_encerra_sozinha_quando_a_coleta_acaba(
    tenant_a: asyncpg.Connection, dados: Dados
) -> None:
    caixa = Caixa()
    campanha_id, _ = await campanhas.abrir(
        tenant_a,
        tenant_id=dados.tenant_a,
        usuario_id=dados.admin_a,
        inventario_id=dados.inventario_a1,
        formulario={"prazo": PRAZO.isoformat()},
        hoje=date(2026, 3, 1),
        enviar=caixa,
    )
    # Fonte fora da materialidade: nada mais falta.
    await tenant_a.execute(
        "UPDATE fonte_emissao SET material = false, justificativa = 'Irrelevante para o teste'"
        " WHERE fonte_id = $1",
        dados.fonte_a1,
    )
    assert await campanhas.lembrar(tenant_a, dados.tenant_a, date(2026, 3, 19), caixa) == 0
    motivo = await tenant_a.fetchval(
        "SELECT motivo_encerramento FROM campanha WHERE campanha_id = $1", campanha_id
    )
    assert motivo == "coleta_completa"


async def test_prazo_no_passado_e_recusado(tenant_a: asyncpg.Connection, dados: Dados) -> None:
    with pytest.raises(ErrosDeValidacao, match="prazo"):
        await campanhas.abrir(
            tenant_a,
            tenant_id=dados.tenant_a,
            usuario_id=dados.admin_a,
            inventario_id=dados.inventario_a1,
            formulario={"prazo": "2026-02-01"},
            hoje=date(2026, 3, 1),
            enviar=Caixa(),
        )
