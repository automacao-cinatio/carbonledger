"""Limites organizacionais do GHG Protocol: quanto de cada unidade entra no inventario.

A unidade guarda os fatos (participacao societaria e controles); a abordagem de
consolidacao e escolhida em cada inventario:

- controle operacional: 100% das unidades em que a empresa tem o controle operacional, 0% nas
  demais;
- controle financeiro: 100% das unidades em que a empresa tem o controle financeiro, 0% nas
  demais;
- participacao societaria: a fracao do capital que a empresa detem na unidade.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

CEM = Decimal(100)


class Abordagem(StrEnum):
    CONTROLE_OPERACIONAL = "controle_operacional"
    CONTROLE_FINANCEIRO = "controle_financeiro"
    PARTICIPACAO_SOCIETARIA = "participacao_societaria"


ROTULOS_ABORDAGEM = {
    Abordagem.CONTROLE_OPERACIONAL: "Controle operacional",
    Abordagem.CONTROLE_FINANCEIRO: "Controle financeiro",
    Abordagem.PARTICIPACAO_SOCIETARIA: "Participação societária",
}


@dataclass(frozen=True)
class FatosDaUnidade:
    participacao_societaria: Decimal
    controle_operacional: bool
    controle_financeiro: bool


def percentual_consolidado(abordagem: Abordagem, unidade: FatosDaUnidade) -> Decimal:
    """Percentual (0 a 100) das emissoes da unidade que entra no inventario."""
    if not (Decimal(0) <= unidade.participacao_societaria <= CEM):
        raise ValueError("participacao societaria fora de 0 a 100")
    if abordagem is Abordagem.CONTROLE_OPERACIONAL:
        return CEM if unidade.controle_operacional else Decimal(0)
    if abordagem is Abordagem.CONTROLE_FINANCEIRO:
        return CEM if unidade.controle_financeiro else Decimal(0)
    return unidade.participacao_societaria


def dentro_do_limite(abordagem: Abordagem, unidade: FatosDaUnidade) -> bool:
    """A unidade entra no inventario (percentual maior que zero). Fora dele, as fontes dela nao
    cobram dado, nao contam no progresso nem geram pendencia no calculo ou no fechamento."""
    return percentual_consolidado(abordagem, unidade) > 0


# O mesmo criterio em SQL, para consultas que juntam fonte_emissao f, unidade u e inventario i.
# tests/isolation/test_limite_organizacional.py confere que bate com dentro_do_limite.
SQL_DENTRO_DO_LIMITE = (
    "(CASE i.abordagem"
    " WHEN 'controle_operacional' THEN u.controle_operacional"
    " WHEN 'controle_financeiro' THEN u.controle_financeiro"
    " ELSE u.participacao_societaria > 0 END)"
)
