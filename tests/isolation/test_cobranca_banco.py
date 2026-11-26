"""Cobranca no banco (P22): fatura do mes uma vez so, vencimento, inadimplencia, pagamento por
evento do gateway (uma vez so), localizacao pelo webhook, periodo de teste e privilegios."""

from collections.abc import AsyncIterator
from datetime import date
from typing import Any
from uuid import UUID

import asyncpg
import pytest

from app.core.tenancy import ContextoTenant, Escopo, aplicar_contexto
from app.modules.cobranca import precos, servico
from app.modules.cobranca.gateway import GatewaySimulado, Pagador, do_simulador
from tests.isolation.conftest import Dados

pytestmark = pytest.mark.integration

PARAMETROS = servico.Parametros(
    faixas=precos.ler_tabela("3:390,00;*:190,00"),
    dias_teste=30,
    dias_vencimento=10,
    dias_tolerancia=15,
)
GATEWAY = GatewaySimulado("http://localhost:8000", "token-de-teste")


async def _em_tenant(conexao: asyncpg.Connection, tenant: UUID) -> AsyncIterator[None]:
    transacao = conexao.transaction()
    await transacao.start()
    try:
        await aplicar_contexto(conexao, ContextoTenant(tenant_id=tenant, escopo=Escopo.TENANT))
        yield
    finally:
        await transacao.rollback()


async def test_ciclo_da_fatura(conexao_app: asyncpg.Connection, dados: Dados) -> None:
    async for _ in _em_tenant(conexao_app, dados.tenant_a):
        # Marco: fatura das 2 empresas ativas, uma vez so.
        marco = await servico.rodada(
            conexao_app, dados.tenant_a, date(2026, 3, 5), PARAMETROS, GATEWAY
        )
        assert marco.fatura_emitida == servico.id_da_fatura(dados.tenant_a, date(2026, 3, 1))
        fatura = await conexao_app.fetchrow(
            "SELECT * FROM fatura WHERE fatura_id = $1", marco.fatura_emitida
        )
        assert fatura["empresas_ativas"] == 2 and fatura["valor_centavos"] == 78000
        assert fatura["vencimento"] == date(2026, 3, 15) and fatura["situacao"] == "aberta"
        assert fatura["url_pagamento"].endswith(
            f"/cobranca/simulador/{fatura['gateway_cobranca_id']}"
        )
        de_novo = await servico.rodada(
            conexao_app, dados.tenant_a, date(2026, 3, 6), PARAMETROS, GATEWAY
        )
        assert de_novo.fatura_emitida is None
        assert (
            await conexao_app.fetchval(
                "SELECT count(*) FROM fatura WHERE competencia = '2026-03-01'"
            )
            == 1
        )

        # Passou do vencimento: vencida, ainda dentro da tolerancia.
        vencida = await servico.rodada(
            conexao_app, dados.tenant_a, date(2026, 3, 20), PARAMETROS, GATEWAY
        )
        assert vencida.vencidas == 1 and vencida.situacao == "ativa"

        # Passou da tolerancia: inadimplente; abril tambem e faturado.
        abril = await servico.rodada(
            conexao_app, dados.tenant_a, date(2026, 4, 1), PARAMETROS, GATEWAY
        )
        assert abril.situacao == "inadimplente" and abril.fatura_emitida is not None
        assert await conexao_app.fetchval("SELECT situacao FROM assinatura") == "inadimplente"

        # O webhook acha o tenant e a fatura pela cobranca (funcao do app_identidade).
        achado = await servico.localizar(conexao_app, "simulado", fatura["gateway_cobranca_id"])
        assert achado == (dados.tenant_a, marco.fatura_emitida)
        assert await servico.localizar(conexao_app, "simulado", "nao-existe") is None

        # Pagamento de marco: paga e, sem outra vencida, a assinatura volta a ativa.
        evento = do_simulador(fatura["gateway_cobranca_id"])
        assert await servico.registrar_evento(
            conexao_app, dados.tenant_a, marco.fatura_emitida, "simulado", evento
        )
        assert (
            await conexao_app.fetchval(
                "SELECT situacao FROM fatura WHERE fatura_id = $1", marco.fatura_emitida
            )
            == "paga"
        )
        assert await conexao_app.fetchval("SELECT situacao FROM assinatura") == "ativa"
        # O mesmo evento de novo nao faz nada.
        assert not await servico.registrar_evento(
            conexao_app, dados.tenant_a, marco.fatura_emitida, "simulado", evento
        )
        acoes = [
            linha["acao"]
            for linha in await conexao_app.fetch(
                "SELECT acao FROM audit_log WHERE acao LIKE 'cobranca.%' ORDER BY seq"
            )
        ]
        assert acoes == [
            "cobranca.fatura_emitida",
            "cobranca.situacao_alterada",
            "cobranca.fatura_emitida",
            "cobranca.fatura_paga",
            "cobranca.situacao_alterada",
        ]


async def test_periodo_de_teste_nao_fatura(conexao_app: asyncpg.Connection, dados: Dados) -> None:
    async for _ in _em_tenant(conexao_app, dados.tenant_b):
        await conexao_app.execute(
            "UPDATE assinatura SET situacao = 'teste', teste_ate = '2030-01-01'"
        )
        rodada = await servico.rodada(
            conexao_app, dados.tenant_b, date(2026, 3, 5), PARAMETROS, GATEWAY
        )
        assert rodada.fatura_emitida is None and rodada.situacao == "teste"


class GatewayQueExigePagador(GatewaySimulado):
    """Como o Asaas: sem pagador nao ha fatura; guarda o que recebeu."""

    nome = "simulado"
    exige_pagador = True

    def __init__(self) -> None:
        super().__init__("http://localhost:8000", "token-de-teste")
        self.criados: list[Pagador | None] = []
        self.atualizados: list[tuple[str, Pagador]] = []

    async def criar_cliente(self, tenant_id: str, nome: str, pagador: Pagador | None) -> str:
        self.criados.append(pagador)
        return "cli_exige"

    async def atualizar_cliente(self, cliente_id: str, pagador: Pagador) -> None:
        self.atualizados.append((cliente_id, pagador))


async def test_pagador_da_cobranca(conexao_app: asyncpg.Connection, dados: Dados) -> None:
    gateway = GatewayQueExigePagador()
    async for _ in _em_tenant(conexao_app, dados.tenant_a):
        # Sem pagador: nenhuma fatura, com o aviso do que fazer.
        sem = await servico.rodada(
            conexao_app, dados.tenant_a, date(2026, 3, 5), PARAMETROS, gateway
        )
        assert sem.fatura_emitida is None and sem.avisos == [servico.AVISO_SEM_PAGADOR]
        assert gateway.criados == []

        primeiro = Pagador("Controladora S.A.", "11222333000181", "financeiro@controladora.example")
        await servico.salvar_pagador(conexao_app, dados.tenant_a, dados.admin_a, primeiro)
        marco = await servico.rodada(
            conexao_app, dados.tenant_a, date(2026, 3, 5), PARAMETROS, gateway
        )
        assert marco.fatura_emitida is not None and gateway.criados == [primeiro]
        assinatura = await conexao_app.fetchrow("SELECT * FROM assinatura")
        assert assinatura["gateway_cliente_id"] == "cli_exige"
        assert assinatura["pagador_sincronizado"] is True

        # Pagador novo: o cliente do gateway e atualizado antes da fatura seguinte, uma vez.
        segundo = Pagador("Outra S.A.", "12ABC34501DE35", "contas@outra.example")
        await servico.salvar_pagador(conexao_app, dados.tenant_a, dados.admin_a, segundo)
        assert await conexao_app.fetchval("SELECT pagador_sincronizado FROM assinatura") is False
        await servico.rodada(conexao_app, dados.tenant_a, date(2026, 4, 2), PARAMETROS, gateway)
        await servico.rodada(conexao_app, dados.tenant_a, date(2026, 5, 2), PARAMETROS, gateway)
        assert gateway.atualizados == [("cli_exige", segundo)]

        # A trilha registra a troca, sem os dados do pagador.
        trilha = await conexao_app.fetch(
            "SELECT dados::text AS dados FROM audit_log WHERE acao = 'cobranca.pagador_alterado'"
        )
        assert len(trilha) == 2
        assert all("financeiro" not in t["dados"] and "11222333" not in t["dados"] for t in trilha)


async def test_pagador_incompleto_ou_cnpj_fora_do_formato(
    conexao_admin: asyncpg.Connection, dados: Dados
) -> None:
    for sql in (
        "UPDATE assinatura SET pagador_nome = 'So nome' WHERE tenant_id = $1",
        "UPDATE assinatura SET pagador_nome = 'X', pagador_cnpj = '123',"
        " pagador_email = 'a@b.example' WHERE tenant_id = $1",
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await conexao_admin.execute(sql, dados.tenant_a)


async def test_fatura_so_muda_situacao_e_gateway(conexao_admin: asyncpg.Connection) -> None:
    for coluna, esperado in (
        ("situacao", True),
        ("paga_em", True),
        ("url_pagamento", True),
        ("valor_centavos", False),
        ("competencia", False),
        ("empresas_ativas", False),
    ):
        tem = await conexao_admin.fetchval(
            "SELECT has_column_privilege('app_user', 'fatura', $1, 'UPDATE')", coluna
        )
        assert tem is esperado, coluna


class GatewayFalso(GatewaySimulado):
    """Gateway controlado pelo teste: falha quando mandado, diz quais referencias estao pagas e
    pode rodar algo no meio da emissao (para testar a trava da assinatura)."""

    def __init__(self, nome: str = "simulado") -> None:
        super().__init__("http://localhost:8000", "token-de-teste")
        self.nome = nome
        self.falhar = False
        self.falhar_conciliacao = False
        self.pagas: set[str] = set()
        self.criados: list[str] = []
        self.no_meio: Any = None

    async def criar_cliente(self, tenant_id: str, nome: str, pagador: Pagador | None) -> str:
        if self.no_meio is not None:
            await self.no_meio()
        if self.falhar:
            raise RuntimeError("gateway fora do ar (simulado)")
        self.criados.append(tenant_id)
        return f"{self.nome}_cli_{tenant_id}"

    async def criar_cobranca(self, *args: Any, **kwargs: Any) -> Any:
        if self.falhar:
            raise RuntimeError("gateway fora do ar (simulado)")
        return await super().criar_cobranca(*args, **kwargs)

    async def cobranca_paga(self, referencia: str) -> bool:
        if self.falhar_conciliacao:
            raise RuntimeError("gateway fora do ar (simulado)")
        return referencia in self.pagas


async def test_falha_do_gateway_nao_desfaz_vencimento(
    conexao_app: asyncpg.Connection, dados: Dados
) -> None:
    gateway = GatewayFalso()
    async for _ in _em_tenant(conexao_app, dados.tenant_a):
        marco = await servico.rodada(
            conexao_app, dados.tenant_a, date(2026, 3, 5), PARAMETROS, gateway
        )
        assert marco.fatura_emitida is not None
        # Abril com o gateway fora: marco vence e passa da tolerancia; a inadimplencia fica
        # gravada, a fatura de abril nao sai e a falha vira aviso.
        gateway.falhar = gateway.falhar_conciliacao = True
        await conexao_app.execute("UPDATE assinatura SET gateway_cliente_id = NULL")
        abril = await servico.rodada(
            conexao_app, dados.tenant_a, date(2026, 4, 1), PARAMETROS, gateway
        )
        assert abril.fatura_emitida is None and abril.situacao == "inadimplente"
        assert abril.avisos == [
            servico.AVISO_GATEWAY_CONCILIACAO,
            servico.AVISO_GATEWAY_EMISSAO,
        ]
        assert await conexao_app.fetchval("SELECT situacao FROM assinatura") == "inadimplente"
        assert (
            await conexao_app.fetchval(
                "SELECT situacao FROM fatura WHERE fatura_id = $1", marco.fatura_emitida
            )
            == "vencida"
        )
        # O passo que falhou foi desfeito inteiro: nem o id de cliente ficou gravado.
        assert await conexao_app.fetchval("SELECT gateway_cliente_id FROM assinatura") is None
        # No dia seguinte, com o gateway de volta, a fatura de abril sai.
        gateway.falhar = gateway.falhar_conciliacao = False
        de_volta = await servico.rodada(
            conexao_app, dados.tenant_a, date(2026, 4, 2), PARAMETROS, gateway
        )
        assert de_volta.fatura_emitida is not None and de_volta.avisos == []


async def test_conciliacao_recupera_pagamento_sem_webhook(
    conexao_app: asyncpg.Connection, dados: Dados
) -> None:
    gateway = GatewayFalso()
    async for _ in _em_tenant(conexao_app, dados.tenant_a):
        marco = await servico.rodada(
            conexao_app, dados.tenant_a, date(2026, 3, 5), PARAMETROS, gateway
        )
        assert marco.fatura_emitida is not None
        await servico.rodada(conexao_app, dados.tenant_a, date(2026, 4, 1), PARAMETROS, gateway)
        assert await conexao_app.fetchval("SELECT situacao FROM assinatura") == "inadimplente"
        # Pagou, mas o webhook se perdeu: a rodada pergunta ao gateway e marca paga.
        gateway.pagas.add(str(marco.fatura_emitida))
        conciliada = await servico.rodada(
            conexao_app, dados.tenant_a, date(2026, 4, 2), PARAMETROS, gateway
        )
        assert conciliada.situacao == "ativa"
        assert (
            await conexao_app.fetchval(
                "SELECT situacao FROM fatura WHERE fatura_id = $1", marco.fatura_emitida
            )
            == "paga"
        )
        origem = await conexao_app.fetchval(
            "SELECT dados->>'origem' FROM audit_log WHERE acao = 'cobranca.fatura_paga'"
        )
        assert origem == "conciliacao"


async def test_troca_de_gateway_cria_cliente_novo(
    conexao_app: asyncpg.Connection, dados: Dados
) -> None:
    asaas = GatewayFalso("asaas")
    async for _ in _em_tenant(conexao_app, dados.tenant_a):
        await conexao_app.execute(
            "UPDATE assinatura SET gateway_cliente_id = 'sim_cli_antigo', pagador_nome = 'P',"
            " pagador_cnpj = '11222333000181', pagador_email = 'p@p.example',"
            " pagador_sincronizado = true"
        )
        rodada = await servico.rodada(
            conexao_app, dados.tenant_a, date(2026, 3, 5), PARAMETROS, asaas
        )
        assert rodada.fatura_emitida is not None and asaas.criados == [str(dados.tenant_a)]
        assinatura = await conexao_app.fetchrow("SELECT * FROM assinatura")
        assert assinatura["gateway"] == "asaas"
        assert assinatura["gateway_cliente_id"] == f"asaas_cli_{dados.tenant_a}"
        assert (
            await conexao_app.fetchval(
                "SELECT dados::text FROM audit_log WHERE acao = 'cobranca.gateway_trocado'"
            )
            is not None
        )


async def test_rodada_trava_a_assinatura(banco: Any, dados: Dados) -> None:
    """Durante a emissao, salvar o pagador espera (aqui, com lock_timeout curto, e recusado):
    a troca nao se perde no meio da rodada."""
    gateway = GatewayFalso()
    tentativas: list[str] = []

    async def salvar_no_meio() -> None:
        outra = await asyncpg.connect(banco.dsn_app)
        try:
            async with outra.transaction():
                await aplicar_contexto(
                    outra, ContextoTenant(tenant_id=dados.tenant_b, escopo=Escopo.TENANT)
                )
                await outra.execute("SET LOCAL lock_timeout = '300ms'")
                try:
                    await outra.execute("UPDATE assinatura SET pagador_sincronizado = false")
                    tentativas.append("gravou")
                except asyncpg.LockNotAvailableError:
                    tentativas.append("esperou")
        finally:
            await outra.close()

    gateway.no_meio = salvar_no_meio
    conexao = await asyncpg.connect(banco.dsn_app)
    try:
        async for _ in _em_tenant(conexao, dados.tenant_b):
            # Sem escrita antes: quem trava a linha e so o FOR UPDATE da rodada.
            await servico.rodada(conexao, dados.tenant_b, date(2026, 3, 5), PARAMETROS, gateway)
    finally:
        await conexao.close()
    assert tentativas == ["esperou"]
