"""CSV exportado: separador ";" (Excel em pt-BR) e celulas que nao viram formula.

Celula que comeca com =, +, -, @, tabulacao ou retorno de carro pode ser executada como
formula pela planilha (injecao de formula). Ela recebe um apostrofo na frente. Numeros saem
ja formatados em pt-BR pelo chamador e nunca comecam com esses caracteres, exceto o sinal de
menos, que tambem e neutralizado: a coleta nao tem quantidade negativa.
"""

import csv
import io
from collections.abc import Iterable, Sequence

INICIOS_PERIGOSOS = ("=", "+", "-", "@", "\t", "\r")
SEPARADOR = ";"


def celula_segura(valor: object) -> str:
    texto = "" if valor is None else str(valor)
    if texto.startswith(INICIOS_PERIGOSOS):
        return "'" + texto
    return texto


def gerar_csv(cabecalho: Sequence[str], linhas: Iterable[Sequence[object]]) -> bytes:
    """CSV em UTF-8 com BOM (o Excel reconhece os acentos), com todas as celulas neutralizadas."""
    saida = io.StringIO()
    escritor = csv.writer(saida, delimiter=SEPARADOR, lineterminator="\r\n")
    escritor.writerow([celula_segura(c) for c in cabecalho])
    for linha in linhas:
        escritor.writerow([celula_segura(c) for c in linha])
    return ("﻿" + saida.getvalue()).encode("utf-8")
