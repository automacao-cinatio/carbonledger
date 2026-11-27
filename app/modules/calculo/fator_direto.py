"""Metodo fator_direto: emissao = quantidade x fator por gas x GWP x consolidacao.

Funcao pura: recebe a entrada ja resolvida (fatores, GWP e mistura vindos da referencia
aprovada) e devolve as linhas da memoria de calculo, as pendencias e os dois hashes. Nada de
banco, relogio ou float: mesma entrada, mesmo resultado, mesmo hash.

Para cada lancamento aprovado:
1. a mistura da data de referencia divide a quantidade entre os componentes (sem mistura, o
   proprio combustivel e o unico componente, com 100%);
2. a quantidade do componente e convertida pelo pint para a unidade do fator (L, m**3, kWh...).
   Grandezas diferentes (kg contra L) viram pendencia: o metodo nao usa densidade nem poder
   calorifico (decisao do usuario; a conversao entra com a ferramenta oficial, no P13);
3. massa do gas = quantidade convertida x fator, em toneladas;
4. a massa entra com o percentual de consolidacao da unidade (abordagem do inventario);
5. tCO2e = massa x GWP do gas no conjunto do inventario. CO2 biogenico fica fora do escopo:
   so a massa, sem GWP, relatada a parte.

Mudar qualquer regra daqui exige versao nova do metodo (VERSAO). O escopo de cada linha vem
do prefixo da categoria (escopo1., escopo2.); cada escopo tem o proprio total.
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

import pint

from app.modules.calculo.unidades import (
    UnidadeInvalida,
    registro,
    unidade,
    validar_unidade_de_fator,
)

METODO = "fator_direto"
VERSAO = "1.3.0"
# Versoes com a mesma conta pura (o que muda entre elas e a montagem da entrada): uma execucao
# gravada com qualquer uma delas pode ser refeita por este codigo.
# 1.0.0: so o escopo 1. 1.1.0: escopo 2 junto, com total separado no resultado.
# 1.2.0: escopo 3 junto, com total separado.
# 1.3.0: fonte de unidade fora do limite organizacional (0%) nao entra nem gera pendencia.
VERSOES_COMPATIVEIS = ("1.0.0", "1.1.0", "1.2.0", "1.3.0")
PRECISAO = 50
CEM = Decimal(100)
TONELADA = "t"


@dataclass(frozen=True)
class FatorUsado:
    fator_versao_id: UUID
    gas: str
    biogenico: bool
    valor: Decimal
    unidade: str
    hash_conteudo: str


@dataclass(frozen=True)
class GwpUsado:
    gwp_versao_id: UUID
    gas: str
    valor: Decimal
    hash_conteudo: str


@dataclass(frozen=True)
class Componente:
    """Parte do combustivel. Sem mistura: o proprio combustivel, percentual None (100%)."""

    codigo: str
    percentual: Decimal | None
    mistura_versao_id: UUID | None
    hash_mistura: str | None
    fatores: tuple[FatorUsado, ...]


@dataclass(frozen=True)
class Item:
    """Um lancamento aprovado, com a referencia ja resolvida para a data dele."""

    fonte_id: UUID
    lancamento_id: UUID
    periodo: int
    categoria: str
    atividade: str
    quantidade: Decimal
    unidade: str
    data_referencia: date
    percentual_consolidado: Decimal
    componentes: tuple[Componente, ...]


@dataclass(frozen=True)
class Pendencia:
    fonte_id: UUID
    periodo: int | None
    motivo: str
    detalhe: str


@dataclass(frozen=True)
class Linha:
    fonte_id: UUID
    lancamento_id: UUID
    periodo: int
    categoria: str
    atividade: str
    componente: str
    percentual_mistura: Decimal | None
    mistura_versao_id: UUID | None
    data_referencia: date
    quantidade: Decimal
    unidade: str
    quantidade_convertida: Decimal
    gas: str
    biogenico: bool
    fator: Decimal
    unidade_fator: str
    fator_versao_id: UUID
    gwp: Decimal | None
    gwp_versao_id: UUID | None
    percentual_consolidado: Decimal
    massa_t: Decimal
    tco2e: Decimal | None


@dataclass(frozen=True)
class Entrada:
    gwp_conjunto: str
    conhecido_em: datetime
    itens: tuple[Item, ...]
    gwps: dict[str, GwpUsado]
    pendencias: tuple[Pendencia, ...] = ()


@dataclass(frozen=True)
class Resultado:
    linhas: tuple[Linha, ...]
    pendencias: tuple[Pendencia, ...]
    escopo1_tco2e: Decimal
    escopo2_tco2e: Decimal
    escopo3_tco2e: Decimal
    biogenico_tco2: Decimal
    hash_entrada: bytes
    hash_resultado: bytes
    metodo: str = METODO
    versao: str = VERSAO


# ---------------------------------------------------------------- hash canonico


# Campos de cada dataclass ja em ordem (a ordem de sorted, como na cadeia de _canonico).
_CAMPOS_ORDENADOS: dict[type, tuple[str, ...]] = {}


def _canonico(valor: Any) -> Any:
    """Forma estavel para o hash: 2,60 e 2,6 dao o mesmo texto; datas em ISO."""
    # Atalho pelo tipo exato dos casos comuns (mesma saida da cadeia abaixo, so mais rapido).
    tipo = type(valor)
    if tipo is str or tipo is int or tipo is bool or valor is None:
        return valor
    if tipo is Decimal:
        normalizado = valor.normalize()
        return format(normalizado, "f") if normalizado != 0 else "0"
    if tipo is dict:
        return {str(k): _canonico(v) for k, v in sorted(valor.items())}
    if tipo is list or tipo is tuple:
        return [_canonico(v) for v in valor]
    if tipo is UUID:
        return str(valor)
    campos = _CAMPOS_ORDENADOS.get(tipo)
    if campos is None and hasattr(valor, "__dataclass_fields__"):
        campos = _CAMPOS_ORDENADOS[tipo] = tuple(sorted(valor.__dataclass_fields__))
    if campos is not None:
        return {nome: _canonico(getattr(valor, nome)) for nome in campos}
    if isinstance(valor, Decimal):
        normalizado = valor.normalize()
        return format(normalizado, "f") if normalizado != 0 else "0"
    if isinstance(valor, bool) or valor is None or isinstance(valor, int | str):
        return valor
    if isinstance(valor, UUID):
        return str(valor)
    if isinstance(valor, datetime | date):
        return valor.isoformat()
    if isinstance(valor, dict):
        return {str(k): _canonico(v) for k, v in sorted(valor.items())}
    if isinstance(valor, list | tuple):
        return [_canonico(v) for v in valor]
    if hasattr(valor, "__dataclass_fields__"):
        return {
            nome: _canonico(getattr(valor, nome)) for nome in sorted(valor.__dataclass_fields__)
        }
    raise TypeError(f"tipo sem forma canonica: {type(valor).__name__}")


def hash_canonico(*partes: Any) -> bytes:
    texto = json.dumps(
        [_canonico(p) for p in partes], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(texto.encode("utf-8")).digest()


# ---------------------------------------------------------------- calculo


def _converter(quantidade: Decimal, de: pint.Unit, para: pint.Unit) -> Decimal:
    convertido = registro().Quantity(quantidade, de).to(para).magnitude
    return Decimal(convertido) if not isinstance(convertido, Decimal) else convertido


def _ordenar_pendencias(pendencias: Iterable[Pendencia]) -> tuple[Pendencia, ...]:
    return tuple(
        sorted(
            pendencias,
            key=lambda p: (
                str(p.fonte_id),
                -1 if p.periodo is None else p.periodo,
                p.motivo,
                p.detalhe,
            ),
        )
    )


def _linhas_do_item(item: Item, gwps: dict[str, GwpUsado]) -> tuple[list[Linha], list[Pendencia]]:
    linhas: list[Linha] = []
    pendencias: list[Pendencia] = []
    try:
        unidade_item = unidade(item.unidade)
    except UnidadeInvalida:
        return [], [Pendencia(item.fonte_id, item.periodo, "unidade_incompativel", item.unidade)]
    tonelada = unidade(TONELADA)
    for componente in item.componentes:
        parte = (
            item.quantidade
            if componente.percentual is None
            else (item.quantidade * componente.percentual / CEM)
        )
        for fator in componente.fatores:
            gas_u, atividade_u = validar_unidade_de_fator(fator.unidade)
            if unidade_item.dimensionality != atividade_u.dimensionality:
                pendencias.append(
                    Pendencia(
                        item.fonte_id,
                        item.periodo,
                        "unidade_incompativel",
                        f"{componente.codigo}: dado em {item.unidade}, fator em {fator.unidade}",
                    )
                )
                continue
            convertida = _converter(parte, unidade_item, atividade_u)
            massa = _converter(convertida * fator.valor, gas_u, tonelada)
            massa_consolidada = massa * item.percentual_consolidado / CEM
            gwp: GwpUsado | None = None
            if not fator.biogenico:
                gwp = gwps.get(fator.gas)
                if gwp is None:
                    pendencias.append(Pendencia(item.fonte_id, item.periodo, "sem_gwp", fator.gas))
                    continue
            linhas.append(
                Linha(
                    fonte_id=item.fonte_id,
                    lancamento_id=item.lancamento_id,
                    periodo=item.periodo,
                    categoria=item.categoria,
                    atividade=item.atividade,
                    componente=componente.codigo,
                    percentual_mistura=componente.percentual,
                    mistura_versao_id=componente.mistura_versao_id,
                    data_referencia=item.data_referencia,
                    quantidade=item.quantidade,
                    unidade=item.unidade,
                    quantidade_convertida=convertida,
                    gas=fator.gas,
                    biogenico=fator.biogenico,
                    fator=fator.valor,
                    unidade_fator=fator.unidade,
                    fator_versao_id=fator.fator_versao_id,
                    gwp=None if gwp is None else gwp.valor,
                    gwp_versao_id=None if gwp is None else gwp.gwp_versao_id,
                    percentual_consolidado=item.percentual_consolidado,
                    massa_t=massa_consolidada,
                    tco2e=None if gwp is None else massa_consolidada * gwp.valor,
                )
            )
    return linhas, pendencias


def _chave_item(item: Item) -> tuple[str, int, str]:
    return (str(item.fonte_id), item.periodo, str(item.lancamento_id))


def escopo_da_categoria(categoria: str) -> int:
    if categoria.startswith("escopo2."):
        return 2
    return 3 if categoria.startswith("escopo3.") else 1


def hash_da_entrada(entrada: Entrada, versao: str = VERSAO) -> bytes:
    """O hash_entrada de calcular(), sem fazer a conta (so depende da entrada).

    Na mesma precisao do calculo: o normalize() do hash canonico depende do contexto.
    """
    with localcontext() as contexto:
        contexto.prec = PRECISAO
        return hash_canonico(
            METODO,
            versao,
            entrada.gwp_conjunto,
            entrada.conhecido_em,
            sorted(entrada.itens, key=_chave_item),
            entrada.gwps,
            _ordenar_pendencias(entrada.pendencias),
        )


def calcular(entrada: Entrada, versao: str = VERSAO) -> Resultado:
    if versao not in VERSOES_COMPATIVEIS:
        raise ValueError(f"versao do metodo desconhecida: {versao}")
    with localcontext() as contexto:
        contexto.prec = PRECISAO
        itens = sorted(entrada.itens, key=_chave_item)
        linhas: list[Linha] = []
        pendencias: list[Pendencia] = list(entrada.pendencias)
        for item in itens:
            novas, problemas = _linhas_do_item(item, entrada.gwps)
            linhas.extend(novas)
            pendencias.extend(problemas)
        ordenadas = _ordenar_pendencias(pendencias)
        escopo1 = sum(
            (
                x.tco2e
                for x in linhas
                if x.tco2e is not None and escopo_da_categoria(x.categoria) == 1
            ),
            Decimal(0),
        )
        escopo2 = sum(
            (
                x.tco2e
                for x in linhas
                if x.tco2e is not None and escopo_da_categoria(x.categoria) == 2
            ),
            Decimal(0),
        )
        escopo3 = sum(
            (
                x.tco2e
                for x in linhas
                if x.tco2e is not None and escopo_da_categoria(x.categoria) == 3
            ),
            Decimal(0),
        )
        biogenico = sum((x.massa_t for x in linhas if x.biogenico), Decimal(0))
        hash_entrada = hash_da_entrada(entrada, versao)
        totais: tuple[Decimal, ...]
        if versao == "1.0.0":
            totais = (escopo1, biogenico)
        elif versao == "1.1.0":
            totais = (escopo1, escopo2, biogenico)
        else:
            totais = (escopo1, escopo2, escopo3, biogenico)
        hash_resultado = hash_canonico(METODO, versao, linhas, ordenadas, *totais)
    return Resultado(
        linhas=tuple(linhas),
        pendencias=ordenadas,
        escopo1_tco2e=escopo1,
        escopo2_tco2e=escopo2,
        escopo3_tco2e=escopo3,
        versao=versao,
        biogenico_tco2=biogenico,
        hash_entrada=hash_entrada,
        hash_resultado=hash_resultado,
    )


# ---------------------------------------------------------------- entrada guardada


def entrada_para_json(entrada: Entrada) -> str:
    """Entrada canonica, guardada no calculo: refazer a conta parte dela."""
    return json.dumps(_canonico(entrada), ensure_ascii=False, sort_keys=True)


def _decimal(valor: Any) -> Decimal | None:
    return None if valor is None else Decimal(str(valor))


def _uuid(valor: Any) -> UUID | None:
    return None if valor is None else UUID(str(valor))


def entrada_de_json(texto: str) -> Entrada:
    bruto = json.loads(texto)
    itens = tuple(
        Item(
            fonte_id=UUID(i["fonte_id"]),
            lancamento_id=UUID(i["lancamento_id"]),
            periodo=int(i["periodo"]),
            categoria=i["categoria"],
            atividade=i["atividade"],
            quantidade=Decimal(i["quantidade"]),
            unidade=i["unidade"],
            data_referencia=date.fromisoformat(i["data_referencia"]),
            percentual_consolidado=Decimal(i["percentual_consolidado"]),
            componentes=tuple(
                Componente(
                    codigo=c["codigo"],
                    percentual=_decimal(c["percentual"]),
                    mistura_versao_id=_uuid(c["mistura_versao_id"]),
                    hash_mistura=c["hash_mistura"],
                    fatores=tuple(
                        FatorUsado(
                            fator_versao_id=UUID(f["fator_versao_id"]),
                            gas=f["gas"],
                            biogenico=bool(f["biogenico"]),
                            valor=Decimal(f["valor"]),
                            unidade=f["unidade"],
                            hash_conteudo=f["hash_conteudo"],
                        )
                        for f in c["fatores"]
                    ),
                )
                for c in i["componentes"]
            ),
        )
        for i in bruto["itens"]
    )
    gwps = {
        gas: GwpUsado(UUID(g["gwp_versao_id"]), g["gas"], Decimal(g["valor"]), g["hash_conteudo"])
        for gas, g in bruto["gwps"].items()
    }
    pendencias = tuple(
        Pendencia(UUID(p["fonte_id"]), p["periodo"], p["motivo"], p["detalhe"])
        for p in bruto["pendencias"]
    )
    return Entrada(
        gwp_conjunto=bruto["gwp_conjunto"],
        conhecido_em=datetime.fromisoformat(bruto["conhecido_em"]),
        itens=itens,
        gwps=gwps,
        pendencias=pendencias,
    )


def somar_por(linhas: Sequence[Linha], chave: str) -> dict[str, Decimal]:
    """Total de tCO2e por categoria, gas ou atividade (sem o biogenico)."""
    totais: dict[str, Decimal] = {}
    for linha in linhas:
        if linha.tco2e is None:
            continue
        nome = str(getattr(linha, chave))
        totais[nome] = totais.get(nome, Decimal(0)) + linha.tco2e
    return totais
