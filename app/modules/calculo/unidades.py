"""Registro unico de unidades (pint), com Decimal. Nenhuma unidade e lida fora daqui.

Unidade de fator: "massa do gas / unidade da atividade", como kg/L, t/MWh ou g/km. O
numerador precisa ser massa; o denominador, uma unidade que o pint conheca.
"""

from decimal import Decimal
from functools import lru_cache

import pint

DIMENSAO_MASSA = "[mass]"


@lru_cache(maxsize=1)
def registro() -> pint.UnitRegistry:
    unidades = pint.UnitRegistry(non_int_type=Decimal, autoconvert_offset_to_baseunit=False)
    # Moeda para fatores por valor gasto (escopo 3): uma grandeza propria, sem conversao entre
    # moedas (cambio nao e unidade). So o real.
    unidades.define("BRL = [moeda]")
    return unidades


class UnidadeInvalida(ValueError):
    """Unidade que o registro nao conhece ou que nao serve para o uso pedido."""


# Cache so da leitura do nome: a unidade do pint e imutavel, entao a conta nao muda. Nome
# invalido levanta excecao, que o lru_cache nao guarda (e recusado de novo a cada vez).
@lru_cache(maxsize=1024)
def unidade(texto: str) -> pint.Unit:
    try:
        resultado = registro().Unit(texto.strip())
    except (
        pint.errors.UndefinedUnitError,
        pint.errors.DefinitionSyntaxError,
        ValueError,
        AttributeError,
        TypeError,
    ) as erro:
        raise UnidadeInvalida(f"unidade desconhecida: {texto!r}") from erro
    return resultado


@lru_cache(maxsize=1024)
def validar_unidade_de_fator(texto: str) -> tuple[pint.Unit, pint.Unit]:
    """Separa "massa/atividade" e confere: (unidade do gas, unidade da atividade)."""
    partes = texto.split("/")
    if len(partes) != 2 or not all(p.strip() for p in partes):
        raise UnidadeInvalida(f"use massa/atividade, como kg/L: {texto!r}")
    gas, atividade = unidade(partes[0]), unidade(partes[1])
    if gas.dimensionality != registro().get_dimensionality(DIMENSAO_MASSA):
        raise UnidadeInvalida(f"o numerador precisa ser massa (g, kg, t): {texto!r}")
    if atividade.dimensionless:
        raise UnidadeInvalida(f"o denominador precisa ser uma unidade de atividade: {texto!r}")
    return gas, atividade


TAMANHO_MAXIMO_UNIDADE = 40


def validar_unidade_de_atividade(texto: str) -> pint.Unit:
    """Unidade do dado de atividade (L, m**3, kg, t, kWh, MWh, GJ, km): nunca adimensional."""
    limpo = texto.strip()
    if not limpo or len(limpo) > TAMANHO_MAXIMO_UNIDADE:
        raise UnidadeInvalida(f"unidade vazia ou longa demais: {texto!r}")
    resultado = unidade(limpo)
    if resultado.dimensionless:
        raise UnidadeInvalida(f"a unidade precisa medir algo (volume, massa, energia): {texto!r}")
    return resultado


def mesma_dimensao(a: str, b: str) -> bool:
    """True quando as duas unidades medem a mesma grandeza (L e m**3; kWh e MWh)."""
    return bool(unidade(a).dimensionality == unidade(b).dimensionality)
