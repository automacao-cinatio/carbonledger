"""Montagem do calculo: fonte de unidade fora do limite organizacional (0%) fica de fora.

Fatores FICTICIOS, so para o teste.
"""

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.modules.calculo import fator_direto, montagem
from app.modules.calculo.fator_direto import FatorUsado, GwpUsado
from app.modules.calculo.montagem import DadoAtual, FonteParaCalculo, ParteDaMistura
from app.modules.organizacao.limites import Abordagem, FatosDaUnidade, dentro_do_limite

FATOR = FatorUsado(uuid4(), "CO2", False, Decimal("2"), "kg/L", "ficticio")
GWP = GwpUsado(uuid4(), "CO2", Decimal(1), "ficticio")


class ReferenciaFicticia:
    async def mistura(self, combustivel: str, quando: date) -> list[ParteDaMistura]:
        return []

    async def fatores(self, categoria: str, codigo: str, quando: date) -> list[FatorUsado]:
        return [FATOR]

    async def gwp(self, conjunto: str, gas: str) -> GwpUsado | None:
        return GWP


def _fonte(participacao: str, operacional: bool, financeiro: bool) -> FonteParaCalculo:
    return FonteParaCalculo(
        fonte_id=uuid4(),
        categoria="escopo1.combustao_estacionaria",
        atividade="teste.diesel",
        periodicidade="anual",
        participacao_societaria=Decimal(participacao),
        controle_operacional=operacional,
        controle_financeiro=financeiro,
    )


def _montar(abordagem: Abordagem, fontes: list[FonteParaCalculo], com_dado: set[UUID]):  # type: ignore[no-untyped-def]
    atuais = {
        (f.fonte_id, 0): DadoAtual(uuid4(), Decimal(1000), "L", None, "aprovado")
        for f in fontes
        if f.fonte_id in com_dado
    }
    return asyncio.run(
        montagem.montar(
            ano=2025,
            abordagem=abordagem,
            gwp_conjunto="AR5_100",
            conhecido_em=datetime(2026, 1, 1, tzinfo=UTC),
            fontes=fontes,
            atuais=atuais,
            referencia=ReferenciaFicticia(),
        )
    )


def test_unidade_sem_controle_nao_pede_dado_nem_gera_pendencia() -> None:
    dentro = _fonte("100", True, True)
    fora = _fonte("40", False, True)  # sem controle operacional
    entrada = _montar(Abordagem.CONTROLE_OPERACIONAL, [dentro, fora], {dentro.fonte_id})
    assert [i.fonte_id for i in entrada.itens] == [dentro.fonte_id]
    assert entrada.pendencias == ()
    resultado = fator_direto.calcular(entrada)
    assert resultado.escopo1_tco2e == Decimal(2)
    assert resultado.versao == "1.3.0"


def test_fora_do_limite_com_dado_tambem_nao_entra() -> None:
    fora = _fonte("0", True, True)  # participacao 0 na abordagem societaria
    entrada = _montar(Abordagem.PARTICIPACAO_SOCIETARIA, [fora], {fora.fonte_id})
    assert entrada.itens == ()
    assert entrada.pendencias == ()


def test_dentro_do_limite_sem_dado_continua_pendente() -> None:
    dentro = _fonte("40", False, False)  # 40% na abordagem societaria
    entrada = _montar(Abordagem.PARTICIPACAO_SOCIETARIA, [dentro], set())
    assert [(p.motivo, p.detalhe) for p in entrada.pendencias] == [("falta_dado", "sem dado")]


@pytest.mark.parametrize(
    ("abordagem", "participacao", "operacional", "financeiro", "esperado"),
    [
        (Abordagem.CONTROLE_OPERACIONAL, "0", True, False, True),
        (Abordagem.CONTROLE_OPERACIONAL, "100", False, True, False),
        (Abordagem.CONTROLE_FINANCEIRO, "100", True, False, False),
        (Abordagem.CONTROLE_FINANCEIRO, "0", False, True, True),
        (Abordagem.PARTICIPACAO_SOCIETARIA, "0", True, True, False),
        (Abordagem.PARTICIPACAO_SOCIETARIA, "0.01", False, False, True),
    ],
)
def test_dentro_do_limite(
    abordagem: Abordagem, participacao: str, operacional: bool, financeiro: bool, esperado: bool
) -> None:
    fatos = FatosDaUnidade(Decimal(participacao), operacional, financeiro)
    assert dentro_do_limite(abordagem, fatos) is esperado
