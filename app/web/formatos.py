"""Numeros e datas no padrao brasileiro (1.234,56 e 20/11/2026).

Float e recusado: quantidade, fator e resultado sao sempre Decimal (ou inteiro).
"""

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# Milhar com ponto em grupos de 3 (opcional) e decimal com virgula.
_NUMERO_BR = re.compile(r"^-?(\d{1,3}(\.\d{3})+|\d+)(,\d+)?$")


class NumeroInvalido(ValueError):
    """Texto que nao e numero no padrao brasileiro."""


def formatar_numero(valor: Decimal | int | None, casas: int = 2) -> str:
    if valor is None:
        return ""
    if isinstance(valor, bool) or not isinstance(valor, Decimal | int):
        raise TypeError(f"use Decimal ou int, nao {type(valor).__name__}")
    quantizado = Decimal(valor).quantize(Decimal(1).scaleb(-casas), rounding=ROUND_HALF_UP)
    sinal = "-" if quantizado < 0 else ""
    inteiro, _, fracao = f"{abs(quantizado):f}".partition(".")
    grupos: list[str] = []
    while len(inteiro) > 3:
        grupos.insert(0, inteiro[-3:])
        inteiro = inteiro[:-3]
    grupos.insert(0, inteiro)
    texto = ".".join(grupos)
    return f"{sinal}{texto},{fracao}" if casas > 0 else f"{sinal}{texto}"


def formatar_numero_exato(valor: Decimal | int | None, minimo: int = 2, maximo: int = 6) -> str:
    """Com as casas que o numero tem (entre minimo e maximo): 0,0015 t nao vira 0,00."""
    if valor is None:
        return ""
    if isinstance(valor, bool) or not isinstance(valor, Decimal | int):
        raise TypeError(f"use Decimal ou int, nao {type(valor).__name__}")
    expoente = Decimal(valor).normalize().as_tuple().exponent
    casas = -expoente if isinstance(expoente, int) and expoente < 0 else 0
    return formatar_numero(valor, max(minimo, min(casas, maximo)))


def ler_numero(texto: str) -> Decimal:
    """Le "1.234,56", "1234,56" ou "1234". Recusa ponto como decimal ("1.5")."""
    limpo = texto.strip().replace(" ", "")
    if not _NUMERO_BR.fullmatch(limpo):
        raise NumeroInvalido(texto)
    try:
        return Decimal(limpo.replace(".", "").replace(",", "."))
    except InvalidOperation as erro:
        raise NumeroInvalido(texto) from erro


def formatar_data(valor: date | datetime | None) -> str:
    if valor is None:
        return ""
    return valor.strftime("%d/%m/%Y")
