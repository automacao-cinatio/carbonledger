"""Importacao, decisao e consulta da referencia de fatores, GWP e misturas (asyncpg).

Importar e decidir exigem a conexao do papel app_curadoria (so a linha de comando). As
consultas funcionam com qualquer papel que leia (app_user nos workers e na API).

Consulta bitemporal: data_referencia escolhe a vigencia; conhecido_em escolhe o que ja estava
aprovado naquele instante. Mesma data, mesmo instante: mesmo fator, mesmo hash.
"""

import hashlib
from dataclasses import astuple, dataclass, fields
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.modules.fatores.arquivos import (
    LEITORES,
    LinhaFator,
    LinhaGwp,
    LinhaMistura,
    hash_conteudo,
)

TIPOS = tuple(LEITORES)


class LoteRepetido(ValueError):
    """O mesmo arquivo (mesmo SHA-256) ja foi importado para este tipo."""


class DecisaoInvalida(ValueError):
    """Lote inexistente, ja decidido, ou rejeicao sem justificativa."""


@dataclass(frozen=True)
class ResultadoImportacao:
    lote_id: UUID
    sha256: str
    inseridas: int
    ja_conhecidas: int


_TABELA = {"gwp": "gwp_versao", "fatores": "fator_versao", "misturas": "mistura_versao"}
_ID = {"gwp": "gwp_versao_id", "fatores": "fator_versao_id", "misturas": "mistura_versao_id"}


async def importar(
    conexao: Any, tipo: str, arquivo: str, conteudo: bytes, importado_por: str
) -> ResultadoImportacao:
    """Valida o arquivo inteiro e grava um lote pendente de decisao. Nada vale antes de aprovar.

    Linha identica (mesmo hash) a uma versao ja aprovada nao e gravada de novo.
    """
    if tipo not in LEITORES:
        raise ValueError(f"tipo desconhecido: {tipo}")
    if not importado_por.strip():
        raise ValueError("informe quem esta importando")
    linhas = LEITORES[tipo](conteudo)
    sha256 = hashlib.sha256(conteudo).hexdigest()
    tabela = _TABELA[tipo]

    async with conexao.transaction():
        if await conexao.fetchval(
            "SELECT 1 FROM lote_importacao WHERE tipo = $1 AND sha256 = $2", tipo, sha256
        ):
            raise LoteRepetido(f"este arquivo ja foi importado ({sha256[:12]})")
        aprovados = {
            linha["hash_conteudo"]
            for linha in await conexao.fetch(
                f"SELECT v.hash_conteudo FROM {tabela} v"  # noqa: S608  # nosec B608
                " JOIN lote_decisao d ON d.lote_id = v.lote_id AND d.decisao = 'aprovado'"
            )
        }
        novas = [(r, hash_conteudo(tipo, r)) for r in linhas]
        novas = [(r, h) for r, h in novas if h not in aprovados]
        lote_id = uuid4()
        await conexao.execute(
            "INSERT INTO lote_importacao (lote_id, tipo, arquivo, sha256, linhas, importado_por)"
            " VALUES ($1, $2, $3, $4, $5, $6)",
            lote_id,
            tipo,
            arquivo,
            sha256,
            len(novas),
            importado_por.strip(),
        )
        if novas:
            colunas = [c.name for c in fields(novas[0][0])]
            nomes = ", ".join([_ID[tipo], "lote_id", *colunas, "hash_conteudo"])
            marcas = ", ".join(f"${i}" for i in range(1, len(colunas) + 4))
            await conexao.executemany(
                f"INSERT INTO {tabela} ({nomes}) VALUES ({marcas})",  # noqa: S608  # nosec B608
                [(uuid4(), lote_id, *astuple(r), h) for r, h in novas],
            )
    return ResultadoImportacao(lote_id, sha256, len(novas), len(linhas) - len(novas))


async def decidir(
    conexao: Any,
    lote_id: UUID,
    *,
    aprovar: bool,
    responsavel: str,
    justificativa: str | None = None,
) -> datetime:
    """Aprova ou rejeita o lote, uma unica vez. Devolve o instante da decisao."""
    if not responsavel.strip():
        raise DecisaoInvalida("informe o responsavel pela decisao")
    if not aprovar and not (justificativa or "").strip():
        raise DecisaoInvalida("rejeicao precisa de justificativa")
    async with conexao.transaction():
        if not await conexao.fetchval("SELECT 1 FROM lote_importacao WHERE lote_id = $1", lote_id):
            raise DecisaoInvalida("lote inexistente")
        if await conexao.fetchval("SELECT 1 FROM lote_decisao WHERE lote_id = $1", lote_id):
            raise DecisaoInvalida("lote ja decidido; decisao nao muda")
        instante: datetime = await conexao.fetchval(
            "INSERT INTO lote_decisao (lote_id, decisao, responsavel, justificativa)"
            " VALUES ($1, $2, $3, $4) RETURNING decidido_em",
            lote_id,
            "aprovado" if aprovar else "rejeitado",
            responsavel.strip(),
            (justificativa or "").strip() or None,
        )
    return instante


async def listar_lotes(conexao: Any) -> list[dict[str, Any]]:
    linhas = await conexao.fetch(
        "SELECT l.lote_id, l.tipo, l.arquivo, l.sha256, l.linhas, l.importado_por,"
        " l.importado_em, d.decisao, d.responsavel, d.decidido_em"
        " FROM lote_importacao l LEFT JOIN lote_decisao d ON d.lote_id = l.lote_id"
        " ORDER BY l.importado_em DESC"
    )
    return [dict(linha) for linha in linhas]


# ---------------------------------------------------------------- consultas para o calculo


async def instante_do_banco(conexao: Any) -> datetime:
    """Instante de conhecimento para gravar no calculo.

    Sempre o relogio do banco, o mesmo que registra as aprovacoes (decidido_em): o relogio de
    quem chama pode estar atrasado e esconder um fator acabado de aprovar.
    """
    instante: datetime = await conexao.fetchval("SELECT clock_timestamp()")
    return instante


@dataclass(frozen=True)
class FatorVigente:
    fator_versao_id: UUID
    codigo: str
    gas: str
    biogenico: bool
    valor: Decimal
    unidade: str
    vigencia_inicio: date
    vigencia_fim: date | None
    fonte: str
    hash_conteudo: str
    aprovado_em: datetime


@dataclass(frozen=True)
class GwpVigente:
    gwp_versao_id: UUID
    conjunto: str
    gas: str
    valor: Decimal
    fonte: str
    hash_conteudo: str
    aprovado_em: datetime


@dataclass(frozen=True)
class ComponenteMistura:
    mistura_versao_id: UUID
    componente: str
    percentual: Decimal
    fonte: str
    hash_conteudo: str
    aprovado_em: datetime


# Consultas por inteiro (sem montar texto): $1 chave, $2 data de referencia, $3 conhecido_em.
# So conta versao de lote aprovado ate conhecido_em; entre as que sobram, a aprovada por ultimo.

_SQL_FATORES = """
SELECT DISTINCT ON (v.gas, v.biogenico)
       v.fator_versao_id, v.codigo, v.gas, v.biogenico, v.valor, v.unidade,
       v.vigencia_inicio, v.vigencia_fim, v.fonte, v.hash_conteudo,
       d.decidido_em AS aprovado_em
FROM fator_versao v
JOIN lote_decisao d
  ON d.lote_id = v.lote_id AND d.decisao = 'aprovado' AND d.decidido_em <= $3
WHERE v.codigo = $1
  AND v.vigencia_inicio <= $2
  AND (v.vigencia_fim IS NULL OR v.vigencia_fim >= $2)
ORDER BY v.gas, v.biogenico, d.decidido_em DESC, v.vigencia_inicio DESC
"""

_SQL_GWP = """
SELECT v.gwp_versao_id, v.conjunto, v.gas, v.valor, v.fonte, v.hash_conteudo,
       d.decidido_em AS aprovado_em
FROM gwp_versao v
JOIN lote_decisao d
  ON d.lote_id = v.lote_id AND d.decisao = 'aprovado' AND d.decidido_em <= $3
WHERE v.conjunto = $1 AND v.gas = $2
ORDER BY d.decidido_em DESC
LIMIT 1
"""

_SQL_MISTURA = """
SELECT DISTINCT ON (v.componente)
       v.mistura_versao_id, v.componente, v.percentual, v.fonte, v.hash_conteudo,
       d.decidido_em AS aprovado_em
FROM mistura_versao v
JOIN lote_decisao d
  ON d.lote_id = v.lote_id AND d.decisao = 'aprovado' AND d.decidido_em <= $3
WHERE v.combustivel = $1
  AND v.vigencia_inicio <= $2
  AND (v.vigencia_fim IS NULL OR v.vigencia_fim >= $2)
ORDER BY v.componente, d.decidido_em DESC, v.vigencia_inicio DESC
"""


async def fatores_da_atividade(
    conexao: Any, codigo: str, data_referencia: date, conhecido_em: datetime
) -> list[FatorVigente]:
    """Um fator por gas (e por CO2 biogenico ou nao): o aprovado por ultimo ate conhecido_em."""
    linhas = await conexao.fetch(_SQL_FATORES, codigo, data_referencia, conhecido_em)
    return [FatorVigente(**dict(linha)) for linha in linhas]


_SQL_FATORES_NA_CATEGORIA = """
SELECT DISTINCT ON (v.gas, v.biogenico)
       v.fator_versao_id, v.codigo, v.gas, v.biogenico, v.valor, v.unidade,
       v.vigencia_inicio, v.vigencia_fim, v.fonte, v.hash_conteudo,
       d.decidido_em AS aprovado_em
FROM fator_versao v
JOIN lote_decisao d
  ON d.lote_id = v.lote_id AND d.decisao = 'aprovado' AND d.decidido_em <= $3
WHERE v.codigo = $1
  AND v.categoria = $4
  AND v.vigencia_inicio <= $2
  AND (v.vigencia_fim IS NULL OR v.vigencia_fim >= $2)
ORDER BY v.gas, v.biogenico, d.decidido_em DESC, v.vigencia_inicio DESC
"""


async def fatores_na_categoria(
    conexao: Any, categoria: str, codigo: str, data_referencia: date, conhecido_em: datetime
) -> list[FatorVigente]:
    """Como fatores_da_atividade, mas so da categoria da fonte: e a consulta do calculo."""
    linhas = await conexao.fetch(
        _SQL_FATORES_NA_CATEGORIA, codigo, data_referencia, conhecido_em, categoria
    )
    return [FatorVigente(**dict(linha)) for linha in linhas]


@dataclass(frozen=True)
class AtividadeConhecida:
    codigo: str
    descricao: str
    mistura: bool


async def atividades_conhecidas(conexao: Any, categoria: str) -> list[AtividadeConhecida]:
    """Codigos que a fonte pode usar: fatores aprovados da categoria e combustiveis com mistura."""
    linhas = await conexao.fetch(
        """
        SELECT DISTINCT ON (codigo) codigo, descricao, mistura FROM (
            SELECT v.codigo, v.descricao, false AS mistura
              FROM fator_versao v
              JOIN lote_decisao d ON d.lote_id = v.lote_id AND d.decisao = 'aprovado'
             WHERE v.categoria = $1
            UNION ALL
            SELECT m.combustivel, m.combustivel, true
              FROM mistura_versao m
              JOIN lote_decisao d ON d.lote_id = m.lote_id AND d.decisao = 'aprovado'
             WHERE $1 LIKE 'escopo1.combustao%'
        ) t
        ORDER BY codigo, mistura DESC
        """,
        categoria,
    )
    return [AtividadeConhecida(**dict(linha)) for linha in linhas]


async def gwp_do_gas(
    conexao: Any, conjunto: str, gas: str, conhecido_em: datetime
) -> GwpVigente | None:
    linha = await conexao.fetchrow(_SQL_GWP, conjunto, gas, conhecido_em)
    return None if linha is None else GwpVigente(**dict(linha))


async def mistura_na_data(
    conexao: Any, combustivel: str, data_compra: date, conhecido_em: datetime
) -> list[ComponenteMistura]:
    """Componentes do combustivel na data da compra, como conhecidos em conhecido_em."""
    linhas = await conexao.fetch(_SQL_MISTURA, combustivel, data_compra, conhecido_em)
    return [ComponenteMistura(**dict(linha)) for linha in linhas]


__all__ = [
    "TIPOS",
    "AtividadeConhecida",
    "ComponenteMistura",
    "DecisaoInvalida",
    "FatorVigente",
    "GwpVigente",
    "LinhaFator",
    "LinhaGwp",
    "LinhaMistura",
    "LoteRepetido",
    "ResultadoImportacao",
    "atividades_conhecidas",
    "decidir",
    "fatores_da_atividade",
    "fatores_na_categoria",
    "gwp_do_gas",
    "importar",
    "instante_do_banco",
    "listar_lotes",
    "mistura_na_data",
]
