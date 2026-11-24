"""Paridade com a ferramenta oficial do Programa Brasileiro GHG Protocol.

Cada caso mora numa pasta (data/paridade/<caso>/) com a entrada, a referencia usada e os
totais calculados na ferramenta oficial. O caso passa pela mesma montagem e pelo mesmo metodo
do calculo de verdade (montagem.montar e fator_direto.calcular), com a referencia lida dos
arquivos do caso em vez do banco. A diferenca de cada total precisa caber na tolerancia da
configuracao (PARIDADE_TOLERANCIA_TCO2E), que nunca se afrouxa para um caso passar.

Arquivos do caso (CSV com ";", decimal com virgula, como em data/):
- parametros.csv: chave;valor (ano, abordagem, gwp_conjunto, ferramenta);
- fontes.csv: fonte;categoria;atividade;periodicidade;participacao_societaria;
  controle_operacional;controle_financeiro;
- dados.csv: fonte;periodo;quantidade;unidade;data_compra (todos tratados como aprovados);
- fatores.csv, misturas.csv e gwp.csv: no formato de data/ (data/README.md);
- esperado.csv: indicador;valor, com escopo1_tco2e, escopo2_tco2e, biogenico_tco2 e, se
  quiser, categoria:<codigo> e gas:<gas> (tCO2e do escopo 1 e 2).
O formato esta descrito em data/README.md.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from app.modules.calculo import fator_direto, montagem
from app.modules.calculo.fator_direto import FatorUsado, GwpUsado, Resultado
from app.modules.calculo.montagem import DadoAtual, FonteParaCalculo, ParteDaMistura
from app.modules.fatores.arquivos import (
    ErroArquivo,
    LinhaFator,
    LinhaGwp,
    LinhaMistura,
    Problema,
    hash_conteudo,
    ler_fatores,
    ler_gwp,
    ler_misturas,
    ler_tabela,
)
from app.modules.organizacao.limites import Abordagem

ARQUIVOS = (
    "parametros.csv",
    "fontes.csv",
    "dados.csv",
    "fatores.csv",
    "misturas.csv",
    "gwp.csv",
    "esperado.csv",
)
COLUNAS_PARAMETROS = ("chave", "valor")
COLUNAS_FONTES = (
    "fonte",
    "categoria",
    "atividade",
    "periodicidade",
    "participacao_societaria",
    "controle_operacional",
    "controle_financeiro",
)
COLUNAS_DADOS = ("fonte", "periodo", "quantidade", "unidade", "data_compra")
COLUNAS_ESPERADO = ("indicador", "valor")
# O caso e uma foto: tudo nele vale como conhecido desde sempre.
INSTANTE_DO_CASO = datetime(2000, 1, 1, tzinfo=UTC)


class CasoInvalido(ValueError):
    """Arquivo do caso faltando ou fora do formato. A mensagem diz qual e onde."""


def _id(caso: str, *partes: str) -> UUID:
    return uuid5(NAMESPACE_URL, "paridade:" + caso + ":" + ":".join(partes))


def _vale(inicio: date, fim: date | None, quando: date) -> bool:
    return inicio <= quando and (fim is None or quando <= fim)


class ReferenciaDoCaso:
    """Fatores, misturas e GWP do caso, todos tratados como aprovados."""

    def __init__(
        self,
        fatores: list[LinhaFator],
        misturas: list[LinhaMistura],
        gwps: list[LinhaGwp],
    ) -> None:
        self._fatores = fatores
        self._misturas = misturas
        self._gwps = gwps

    async def mistura(self, combustivel: str, quando: date) -> list[ParteDaMistura]:
        return [
            ParteDaMistura(
                m.componente,
                m.percentual,
                None,
                hash_conteudo("misturas", m),
            )
            for m in sorted(self._misturas, key=lambda m: m.componente)
            if m.combustivel == combustivel and _vale(m.vigencia_inicio, m.vigencia_fim, quando)
        ]

    async def fatores(self, categoria: str, codigo: str, quando: date) -> list[FatorUsado]:
        achados = []
        for f in self._fatores:
            if (
                f.categoria == categoria
                and f.codigo == codigo
                and _vale(f.vigencia_inicio, f.vigencia_fim, quando)
            ):
                hash_fator = hash_conteudo("fatores", f)
                achados.append(
                    FatorUsado(
                        uuid5(NAMESPACE_URL, "fator:" + hash_fator),
                        f.gas,
                        f.biogenico,
                        f.valor,
                        f.unidade,
                        hash_fator,
                    )
                )
        return sorted(achados, key=lambda f: (f.gas, f.biogenico))

    async def gwp(self, conjunto: str, gas: str) -> GwpUsado | None:
        for g in self._gwps:
            if g.conjunto == conjunto and g.gas == gas:
                hash_gwp = hash_conteudo("gwp", g)
                return GwpUsado(uuid5(NAMESPACE_URL, "gwp:" + hash_gwp), gas, g.valor, hash_gwp)
        return None


@dataclass(frozen=True)
class Caso:
    nome: str
    ferramenta: str
    resultado: Resultado
    esperado: dict[str, Decimal]


@dataclass(frozen=True)
class Divergencia:
    indicador: str
    esperado: Decimal
    calculado: Decimal

    @property
    def diferenca(self) -> Decimal:
        return abs(self.calculado - self.esperado)

    def __str__(self) -> str:
        return (
            f"{self.indicador}: ferramenta {self.esperado}, CarbonLedger {self.calculado}"
            f" (diferença {self.diferenca})"
        )


def _ler(pasta: Path, nome: str) -> bytes:
    caminho = pasta / nome
    if not caminho.is_file():
        raise CasoInvalido(f"{pasta.name}: falta o arquivo {nome}")
    return caminho.read_bytes()


def _falhar_se(pasta: Path, arquivo: str, problemas: list[Problema]) -> None:
    if problemas:
        raise CasoInvalido(f"{pasta.name}/{arquivo}: " + "; ".join(str(p) for p in problemas[:20]))


def _tabela(pasta: Path, arquivo: str, colunas: tuple[str, ...]) -> list[dict[str, str]]:
    try:
        linhas, problemas = ler_tabela(colunas, _ler(pasta, arquivo))
    except ErroArquivo as erro:
        raise CasoInvalido(f"{pasta.name}/{arquivo}: {erro}") from erro
    _falhar_se(pasta, arquivo, problemas)
    return [{c: (linha.celulas.get(c) or "").strip() for c in colunas} for linha in linhas]


def _referencia(pasta: Path, arquivo: str, leitor: object) -> list[object]:
    try:
        return list(leitor(_ler(pasta, arquivo)))  # type: ignore[operator]
    except ErroArquivo as erro:
        raise CasoInvalido(f"{pasta.name}/{arquivo}: {erro}") from erro


def _decimal(pasta: Path, arquivo: str, texto: str) -> Decimal:
    linhas, problemas = ler_tabela(("v",), f"v\n{texto}\n".encode())
    numero = linhas[0].decimal("v") if linhas else Decimal(0)
    _falhar_se(pasta, arquivo, problemas)
    return numero


def _data(pasta: Path, arquivo: str, texto: str) -> date | None:
    if not texto:
        return None
    linhas, problemas = ler_tabela(("v",), f"v\n{texto}\n".encode())
    valor = linhas[0].data("v") if linhas else None
    _falhar_se(pasta, arquivo, problemas)
    return valor


def _sim(texto: str) -> bool:
    return texto.strip().lower() in ("sim", "s")


async def carregar_caso(pasta: Path) -> Caso:
    nome = pasta.name
    parametros = {
        p["chave"]: p["valor"] for p in _tabela(pasta, "parametros.csv", COLUNAS_PARAMETROS)
    }
    for chave in ("ano", "abordagem", "gwp_conjunto", "ferramenta"):
        if not parametros.get(chave):
            raise CasoInvalido(f"{nome}/parametros.csv: falta a chave {chave}")
    try:
        abordagem = Abordagem(parametros["abordagem"])
        ano = int(parametros["ano"])
    except ValueError as erro:
        raise CasoInvalido(f"{nome}/parametros.csv: {erro}") from erro

    fontes: dict[str, FonteParaCalculo] = {}
    for f in _tabela(pasta, "fontes.csv", COLUNAS_FONTES):
        fontes[f["fonte"]] = FonteParaCalculo(
            fonte_id=_id(nome, "fonte", f["fonte"]),
            categoria=f["categoria"],
            atividade=f["atividade"] or None,
            periodicidade=f["periodicidade"],
            participacao_societaria=_decimal(pasta, "fontes.csv", f["participacao_societaria"]),
            controle_operacional=_sim(f["controle_operacional"]),
            controle_financeiro=_sim(f["controle_financeiro"]),
        )
    atuais: dict[tuple[UUID, int], DadoAtual] = {}
    for d in _tabela(pasta, "dados.csv", COLUNAS_DADOS):
        fonte = fontes.get(d["fonte"])
        if fonte is None:
            raise CasoInvalido(f"{nome}/dados.csv: fonte {d['fonte']!r} nao esta em fontes.csv")
        periodo = 0 if d["periodo"].lower() == "ano" else int(d["periodo"])
        atuais[(fonte.fonte_id, periodo)] = DadoAtual(
            lancamento_id=_id(nome, "dado", d["fonte"], str(periodo)),
            quantidade=_decimal(pasta, "dados.csv", d["quantidade"]),
            unidade=d["unidade"],
            data_compra=_data(pasta, "dados.csv", d["data_compra"]),
            decisao="aprovado",
        )
    referencia = ReferenciaDoCaso(
        _referencia(pasta, "fatores.csv", ler_fatores),  # type: ignore[arg-type]
        _referencia(pasta, "misturas.csv", ler_misturas),  # type: ignore[arg-type]
        _referencia(pasta, "gwp.csv", ler_gwp),  # type: ignore[arg-type]
    )
    entrada = await montagem.montar(
        ano=ano,
        abordagem=abordagem,
        gwp_conjunto=parametros["gwp_conjunto"],
        conhecido_em=INSTANTE_DO_CASO,
        fontes=list(fontes.values()),
        atuais=atuais,
        referencia=referencia,
    )
    esperado = {
        e["indicador"]: _decimal(pasta, "esperado.csv", e["valor"])
        for e in _tabela(pasta, "esperado.csv", COLUNAS_ESPERADO)
    }
    if not esperado:
        raise CasoInvalido(f"{nome}/esperado.csv: nenhum indicador")
    return Caso(nome, parametros["ferramenta"], fator_direto.calcular(entrada), esperado)


def indicadores(resultado: Resultado) -> dict[str, Decimal]:
    valores = {
        "escopo1_tco2e": resultado.escopo1_tco2e,
        "escopo2_tco2e": resultado.escopo2_tco2e,
        "escopo3_tco2e": resultado.escopo3_tco2e,
        "biogenico_tco2": resultado.biogenico_tco2,
    }
    for chave, prefixo in (("categoria", "categoria:"), ("gas", "gas:")):
        for nome, total in fator_direto.somar_por(resultado.linhas, chave).items():
            valores[prefixo + nome] = total
    return valores


def comparar(caso: Caso, tolerancia: Decimal) -> list[Divergencia]:
    """Indicadores do caso fora da tolerancia. Indicador que o calculo nao produz conta como 0."""
    calculados = indicadores(caso.resultado)
    return [
        Divergencia(indicador, esperado, calculados.get(indicador, Decimal(0)))
        for indicador, esperado in sorted(caso.esperado.items())
        if abs(calculados.get(indicador, Decimal(0)) - esperado) > tolerancia
    ]


def pastas_de_casos(raiz: Path) -> list[Path]:
    if not raiz.is_dir():
        return []
    return sorted(p for p in raiz.iterdir() if p.is_dir() and not p.name.startswith("."))


def hash_do_caso(pasta: Path) -> str:
    """SHA-256 dos arquivos do caso: identifica a versao do caso no relatorio de paridade."""
    resumo = hashlib.sha256()
    for nome in ARQUIVOS:
        caminho = pasta / nome
        if caminho.is_file():
            resumo.update(nome.encode() + b"\0" + caminho.read_bytes())
    return resumo.hexdigest()
