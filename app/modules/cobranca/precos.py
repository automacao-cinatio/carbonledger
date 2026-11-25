"""Tabela de precos por empresa ativa por mes, progressiva por faixas (P22).

A tabela vem da configuracao (COBRANCA_TABELA), nunca do codigo: "3:390,00;10:290,00;*:190,00"
quer dizer as empresas 1 a 3 a R$ 390,00 cada, da 4 a 10 a R$ 290,00 cada e da 11 em diante a
R$ 190,00 cada. Valores em centavos (int), sem float.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class TabelaInvalida(ValueError):
    """COBRANCA_TABELA mal escrita. A mensagem diz o que corrigir."""


@dataclass(frozen=True)
class Faixa:
    ate: int | None  # None: sem limite (a ultima faixa)
    preco_centavos: int


@dataclass(frozen=True)
class Linha:
    de: int
    ate: int
    empresas: int
    preco_centavos: int
    subtotal_centavos: int


def _centavos(texto: str) -> int:
    limpo = texto.strip().replace(".", "").replace(",", ".")
    try:
        valor = Decimal(limpo)
    except InvalidOperation as erro:
        raise TabelaInvalida(f"preco invalido: {texto!r} (use 390,00)") from erro
    if valor < 0 or valor != valor.quantize(Decimal("0.01")):
        raise TabelaInvalida(f"preco invalido: {texto!r} (zero ou mais, ate centavos)")
    return int(valor * 100)


def ler_tabela(texto: str) -> list[Faixa]:
    faixas: list[Faixa] = []
    for parte in (p.strip() for p in texto.split(";") if p.strip()):
        limite, separador, preco = parte.partition(":")
        if not separador:
            raise TabelaInvalida(f"faixa sem ':': {parte!r} (use ate:preco)")
        if limite.strip() == "*":
            faixas.append(Faixa(None, _centavos(preco)))
            continue
        if not limite.strip().isdigit() or int(limite) < 1:
            raise TabelaInvalida(f"limite invalido: {limite!r} (numero inteiro de 1 em diante)")
        faixas.append(Faixa(int(limite), _centavos(preco)))
    if not faixas or faixas[-1].ate is not None:
        raise TabelaInvalida("a ultima faixa precisa ser '*' (sem limite)")
    limites = [f.ate for f in faixas[:-1]]
    if any(f.ate is None for f in faixas[:-1]) or limites != sorted(set(limites)):  # type: ignore[type-var]
        raise TabelaInvalida("os limites precisam ser crescentes e so a ultima faixa e '*'")
    return faixas


def calcular(faixas: list[Faixa], empresas: int) -> tuple[int, list[Linha]]:
    """Valor do mes em centavos e o detalhamento por faixa (so as faixas usadas)."""
    if empresas < 0:
        raise ValueError("empresas nao pode ser negativo")
    linhas: list[Linha] = []
    inicio = 1
    for faixa in faixas:
        if inicio > empresas:
            break
        fim = empresas if faixa.ate is None else min(faixa.ate, empresas)
        if fim >= inicio:
            quantidade = fim - inicio + 1
            linhas.append(
                Linha(inicio, fim, quantidade, faixa.preco_centavos,
                      quantidade * faixa.preco_centavos)
            )  # fmt: skip
        inicio = (faixa.ate or empresas) + 1
    return sum(linha.subtotal_centavos for linha in linhas), linhas
