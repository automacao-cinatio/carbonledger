"""Leitura e validacao dos arquivos de referencia em data/ (GWP, fatores e misturas).

Formato comum: CSV em UTF-8 (com ou sem BOM), separador ";", decimal com virgula (1.234,56
ou 1,2E-05), datas em dd/mm/aaaa ou aaaa-mm-dd, sim ou nao para marcacoes. Linha comecando
com # e comentario. Toda linha leva a fonte. Todos os erros saem juntos, com linha e coluna.
O formato de cada tipo esta em data/README.md.
"""

import csv
import hashlib
import io
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.modules.calculo.unidades import UnidadeInvalida, validar_unidade_de_fator

SEPARADOR = ";"
_DECIMAL = re.compile(r"^-?(\d{1,3}(\.\d{3})+|\d+)(,\d+)?([eE][+-]?\d+)?$")
_CODIGO = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
_GAS = re.compile(r"^[A-Z0-9][A-Za-z0-9-]*$")
_CONJUNTO = re.compile(r"^[A-Z0-9_]+$")
CEM = Decimal(100)

COLUNAS = {
    "gwp": ("conjunto", "gas", "valor", "fonte"),
    "fatores": (
        "codigo",
        "descricao",
        "categoria",
        "gas",
        "biogenico",
        "valor",
        "unidade",
        "vigencia_inicio",
        "vigencia_fim",
        "fonte",
        "referencia",
    ),
    "misturas": (
        "combustivel",
        "componente",
        "percentual",
        "vigencia_inicio",
        "vigencia_fim",
        "fonte",
    ),
}


@dataclass(frozen=True)
class Problema:
    linha: int
    coluna: str
    mensagem: str

    def __str__(self) -> str:
        return f"linha {self.linha}, coluna {self.coluna}: {self.mensagem}"


class ErroArquivo(ValueError):
    def __init__(self, problemas: list[Problema]) -> None:
        super().__init__("\n".join(str(p) for p in problemas))
        self.problemas = problemas


def _canonico(valor: Any) -> str:
    if isinstance(valor, Decimal):
        normalizado = valor.normalize()
        return format(normalizado, "f") if normalizado != 0 else "0"
    if isinstance(valor, bool):
        return "sim" if valor else "nao"
    if isinstance(valor, date):
        return valor.isoformat()
    return "" if valor is None else str(valor)


def hash_conteudo(tipo: str, registro: Any) -> str:
    """SHA-256 do conteudo canonico: 2,60 e 2,6 dao o mesmo hash; a fonte entra no hash."""
    partes = [tipo, "v1"] + [
        f"{c.name}={_canonico(getattr(registro, c.name))}" for c in fields(registro)
    ]
    return hashlib.sha256("|".join(partes).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LinhaGwp:
    conjunto: str
    gas: str
    valor: Decimal
    fonte: str


@dataclass(frozen=True)
class LinhaFator:
    codigo: str
    descricao: str
    categoria: str
    gas: str
    biogenico: bool
    valor: Decimal
    unidade: str
    vigencia_inicio: date
    vigencia_fim: date | None
    fonte: str
    referencia: str | None


@dataclass(frozen=True)
class LinhaMistura:
    combustivel: str
    componente: str
    percentual: Decimal
    vigencia_inicio: date
    vigencia_fim: date | None
    fonte: str


# ---------------------------------------------------------------- celulas


class _Linha:
    """Le as celulas de uma linha e junta os problemas, sem parar no primeiro."""

    def __init__(self, numero: int, celulas: dict[str, str], problemas: list[Problema]) -> None:
        self.numero = numero
        self.celulas = celulas
        self.problemas = problemas
        self.ok = True

    def _erro(self, coluna: str, mensagem: str) -> None:
        self.problemas.append(Problema(self.numero, coluna, mensagem))
        self.ok = False

    def texto(self, coluna: str, *, obrigatorio: bool = True) -> str | None:
        valor = (self.celulas.get(coluna) or "").strip()
        if not valor:
            if obrigatorio:
                self._erro(coluna, "vazio")
            return None
        return valor

    def padrao(self, coluna: str, regex: re.Pattern[str], explicacao: str) -> str:
        valor = self.texto(coluna) or ""
        if valor and not regex.fullmatch(valor):
            self._erro(coluna, f"{valor!r} fora do formato ({explicacao})")
        return valor

    def decimal(self, coluna: str) -> Decimal:
        valor = self.texto(coluna)
        if valor is None:
            return Decimal(0)
        if not _DECIMAL.fullmatch(valor):
            self._erro(coluna, f"{valor!r} nao e numero no padrao 1.234,56 ou 1,2E-05")
            return Decimal(0)
        try:
            return Decimal(valor.replace(".", "").replace(",", "."))
        except InvalidOperation:
            self._erro(coluna, f"{valor!r} nao e numero")
            return Decimal(0)

    def data(self, coluna: str, *, obrigatorio: bool = True) -> date | None:
        valor = self.texto(coluna, obrigatorio=obrigatorio)
        if valor is None:
            return None
        for formato in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(valor, formato).date()
            except ValueError:
                continue
        self._erro(coluna, f"{valor!r} nao e data (use dd/mm/aaaa ou aaaa-mm-dd)")
        return None

    def marcacao(self, coluna: str) -> bool:
        valor = (self.texto(coluna) or "").lower()
        if valor in ("sim", "s"):
            return True
        if valor in ("nao", "não", "n"):
            return False
        if valor:
            self._erro(coluna, f"{valor!r}: use sim ou nao")
        return False


LinhaLida = _Linha


def _linhas(tipo: str, conteudo: bytes) -> tuple[list[_Linha], list[Problema]]:
    return ler_tabela(COLUNAS[tipo], conteudo)


def ler_tabela(colunas: Sequence[str], conteudo: bytes) -> tuple[list[_Linha], list[Problema]]:
    """CSV no formato de data/ (";", decimal com virgula), com o cabecalho exato pedido.

    Usado pelos arquivos de referencia e pelos casos de paridade (data/paridade).
    """
    problemas: list[Problema] = []
    try:
        texto = conteudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ErroArquivo([Problema(0, "-", "o arquivo precisa estar em UTF-8")]) from None
    leitor = csv.reader(io.StringIO(texto), delimiter=SEPARADOR)
    cabecalho: list[str] | None = None
    linhas: list[_Linha] = []
    for celulas in leitor:
        if not celulas or not "".join(celulas).strip() or celulas[0].lstrip().startswith("#"):
            continue
        if cabecalho is None:
            cabecalho = [c.strip().lower() for c in celulas]
            esperado = set(colunas)
            faltam, sobram = esperado - set(cabecalho), set(cabecalho) - esperado
            if faltam or sobram or len(cabecalho) != len(set(cabecalho)):
                raise ErroArquivo(
                    [
                        Problema(
                            leitor.line_num,
                            "cabecalho",
                            f"esperado {';'.join(colunas)}"
                            + (f"; faltam {sorted(faltam)}" if faltam else "")
                            + (f"; sobram {sorted(sobram)}" if sobram else ""),
                        )
                    ]
                )
            continue
        if len(celulas) != len(cabecalho):
            problemas.append(
                Problema(leitor.line_num, "-", f"{len(celulas)} colunas; esperado {len(cabecalho)}")
            )
            continue
        linhas.append(
            _Linha(leitor.line_num, dict(zip(cabecalho, celulas, strict=True)), problemas)
        )
    if cabecalho is None:
        raise ErroArquivo([Problema(0, "cabecalho", "arquivo sem cabecalho")])
    return linhas, problemas


def _sobrepoe(a: tuple[date, date | None], b: tuple[date, date | None]) -> bool:
    fim_a, fim_b = a[1] or date.max, b[1] or date.max
    return a[0] <= fim_b and b[0] <= fim_a


# ---------------------------------------------------------------- leitores


def ler_gwp(conteudo: bytes) -> list[LinhaGwp]:
    linhas, problemas = _linhas("gwp", conteudo)
    resultado: list[LinhaGwp] = []
    vistos: dict[tuple[str, str], int] = {}
    for linha in linhas:
        registro = LinhaGwp(
            conjunto=linha.padrao("conjunto", _CONJUNTO, "maiusculas, numeros e _, como AR5_100"),
            gas=linha.padrao("gas", _GAS, "como CO2, CH4, N2O ou HFC-134a"),
            valor=linha.decimal("valor"),
            fonte=linha.texto("fonte") or "",
        )
        if linha.ok and registro.valor <= 0:
            linha._erro("valor", "o GWP precisa ser maior que zero")
        chave = (registro.conjunto, registro.gas)
        if linha.ok and chave in vistos:
            linha._erro("gas", f"repetido (ja na linha {vistos[chave]})")
        if linha.ok:
            vistos[chave] = linha.numero
            resultado.append(registro)
    if problemas:
        raise ErroArquivo(problemas)
    return resultado


def ler_fatores(conteudo: bytes) -> list[LinhaFator]:
    linhas, problemas = _linhas("fatores", conteudo)
    resultado: list[LinhaFator] = []
    por_chave: dict[tuple[str, str, str, bool], list[tuple[int, tuple[date, date | None]]]] = (
        defaultdict(list)
    )
    for linha in linhas:
        unidade = linha.texto("unidade") or ""
        if unidade:
            try:
                validar_unidade_de_fator(unidade)
            except UnidadeInvalida as erro:
                linha._erro("unidade", str(erro))
        inicio = linha.data("vigencia_inicio")
        fim = linha.data("vigencia_fim", obrigatorio=False)
        registro = LinhaFator(
            codigo=linha.padrao("codigo", _CODIGO, "minusculas, numeros, _ e pontos"),
            descricao=linha.texto("descricao") or "",
            categoria=linha.padrao("categoria", _CODIGO, "minusculas, numeros, _ e pontos"),
            gas=linha.padrao("gas", _GAS, "como CO2, CH4, N2O ou HFC-134a"),
            biogenico=linha.marcacao("biogenico"),
            valor=linha.decimal("valor"),
            unidade=unidade,
            vigencia_inicio=inicio or date.min,
            vigencia_fim=fim,
            fonte=linha.texto("fonte") or "",
            referencia=linha.texto("referencia", obrigatorio=False),
        )
        if linha.ok and registro.valor < 0:
            linha._erro("valor", "o fator nao pode ser negativo")
        # So o CO2 de biomassa fica fora dos escopos. CH4 e N2O de biocombustivel entram com GWP:
        # marcados como biogenicos, sairiam do escopo e somariam massa de outro gas ao CO2.
        if linha.ok and registro.biogenico and registro.gas != "CO2":
            linha._erro("biogenico", "so o CO2 e biogenico; para CH4 e N2O de biomassa, use nao")
        if linha.ok and fim is not None and fim < registro.vigencia_inicio:
            linha._erro("vigencia_fim", "termina antes de comecar")
        if not linha.ok:
            continue
        # O mesmo componente tem fatores diferentes por categoria (combustao movel e estacionaria).
        chave = (registro.categoria, registro.codigo, registro.gas, registro.biogenico)
        periodo = (registro.vigencia_inicio, registro.vigencia_fim)
        conflito = next((n for n, p in por_chave[chave] if _sobrepoe(p, periodo)), None)
        if conflito is not None:
            linha._erro(
                "vigencia_inicio",
                f"vigencia sobreposta a da linha {conflito} para o mesmo fator e gas",
            )
            continue
        por_chave[chave].append((linha.numero, periodo))
        resultado.append(registro)
    if problemas:
        raise ErroArquivo(problemas)
    return resultado


def ler_misturas(conteudo: bytes) -> list[LinhaMistura]:
    linhas, problemas = _linhas("misturas", conteudo)
    resultado: list[LinhaMistura] = []
    vistos: dict[tuple[str, str, date], int] = {}
    somas: dict[tuple[str, date], tuple[Decimal, int]] = {}
    for linha in linhas:
        inicio = linha.data("vigencia_inicio")
        fim = linha.data("vigencia_fim", obrigatorio=False)
        registro = LinhaMistura(
            combustivel=linha.padrao("combustivel", _CODIGO, "minusculas, numeros, _ e pontos"),
            componente=linha.padrao("componente", _CODIGO, "minusculas, numeros, _ e pontos"),
            percentual=linha.decimal("percentual"),
            vigencia_inicio=inicio or date.min,
            vigencia_fim=fim,
            fonte=linha.texto("fonte") or "",
        )
        if linha.ok and not Decimal(0) <= registro.percentual <= CEM:
            linha._erro("percentual", "fica entre 0 e 100")
        if linha.ok and fim is not None and fim < registro.vigencia_inicio:
            linha._erro("vigencia_fim", "termina antes de comecar")
        chave = (registro.combustivel, registro.componente, registro.vigencia_inicio)
        if linha.ok and chave in vistos:
            linha._erro("componente", f"repetido (ja na linha {vistos[chave]})")
        if not linha.ok:
            continue
        vistos[chave] = linha.numero
        chave_soma = (registro.combustivel, registro.vigencia_inicio)
        soma, _ = somas.get(chave_soma, (Decimal(0), linha.numero))
        somas[chave_soma] = (soma + registro.percentual, linha.numero)
        if soma + registro.percentual > CEM:
            linha._erro("percentual", "a soma dos componentes deste combustivel passa de 100")
            continue
        resultado.append(registro)
    if problemas:
        raise ErroArquivo(problemas)
    return resultado


LEITORES: dict[str, Callable[[bytes], Sequence[Any]]] = {
    "gwp": ler_gwp,
    "fatores": ler_fatores,
    "misturas": ler_misturas,
}
