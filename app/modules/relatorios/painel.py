"""Painel consolidado: emissoes e andamento de todas as empresas visiveis, e a serie anual.

Le com o RLS de quem esta logado (o responsavel ve so as empresas dele). Para cada inventario,
o numero vem do calculo do fechamento atual quando o inventario esta fechado; senao, do ultimo
calculo, marcado como preliminar. Sem calculo, a empresa aparece sem numero (nunca zero).
"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.coleta import servico as coleta


@dataclass(frozen=True)
class LinhaEmpresa:
    empresa_id: UUID
    empresa: str
    grupo: str
    ano: int | None
    estado: str | None
    coleta: tuple[int, int]
    escopo1: Decimal | None
    escopo2: Decimal | None
    escopo3: Decimal | None
    preliminar: bool


@dataclass(frozen=True)
class AnoConsolidado:
    ano: int
    escopo1: Decimal
    escopo2: Decimal
    escopo3: Decimal
    empresas: int
    preliminar: bool


# Numero de cada inventario: calculo do fechamento atual, ou o ultimo calculo.
_SQL_NUMEROS = """
SELECT i.inventario_id, i.empresa_id, i.ano, i.estado,
       c.escopo1_tco2e, c.escopo2_tco2e, c.escopo3_tco2e
FROM inventario i
LEFT JOIN LATERAL (SELECT coalesce(
    (SELECT e.calculo_id FROM inventario_evento e
      WHERE e.tenant_id = i.tenant_id AND e.inventario_id = i.inventario_id
        AND e.tipo = 'fechamento' AND i.estado = 'fechado'
      ORDER BY e.versao DESC LIMIT 1),
    (SELECT c2.calculo_id FROM calculo c2
      WHERE c2.tenant_id = i.tenant_id AND c2.inventario_id = i.inventario_id
      ORDER BY c2.calculado_em DESC LIMIT 1)) AS calculo_id) x ON true
LEFT JOIN calculo c ON c.tenant_id = i.tenant_id AND c.calculo_id = x.calculo_id
"""


async def grupos(sessao: AsyncSession) -> list[tuple[str, str]]:
    linhas = await sessao.execute(
        text(
            "SELECT DISTINCT g.grupo_id, g.nome FROM grupo g JOIN empresa e"
            " ON e.tenant_id = g.tenant_id AND e.grupo_id = g.grupo_id AND e.ativa"
            " ORDER BY g.nome"
        )
    )
    return [(str(linha.grupo_id), linha.nome) for linha in linhas]


async def montar(
    sessao: AsyncSession, grupo_id: UUID | None
) -> tuple[list[LinhaEmpresa], list[AnoConsolidado]]:
    empresas = list(
        await sessao.execute(
            text(
                "SELECT e.empresa_id, e.nome, g.nome AS grupo FROM empresa e JOIN grupo g"
                " ON g.tenant_id = e.tenant_id AND g.grupo_id = e.grupo_id"
                " WHERE e.ativa AND (CAST(:grupo AS uuid) IS NULL OR e.grupo_id = :grupo)"
                " ORDER BY g.nome, e.nome"
            ),
            {"grupo": grupo_id},
        )
    )
    ids = [e.empresa_id for e in empresas]
    numeros = list(
        await sessao.execute(
            text(_SQL_NUMEROS + " WHERE i.empresa_id = ANY(:ids) ORDER BY i.ano DESC"),
            {"ids": ids},
        )
    )
    por_empresa: dict[UUID, list[Any]] = defaultdict(list)
    for numero in numeros:
        por_empresa[numero.empresa_id].append(numero)

    ultimos = {e.empresa_id: next(iter(por_empresa.get(e.empresa_id, [])), None) for e in empresas}
    # Uma leitura para todas as empresas (antes, tres consultas por empresa).
    progressos = await coleta.progresso_dos_inventarios(
        sessao, [u.inventario_id for u in ultimos.values() if u is not None]
    )
    linhas: list[LinhaEmpresa] = []
    for empresa in empresas:
        ultimo = ultimos[empresa.empresa_id]
        progresso = (0, 0)
        if ultimo is not None:
            total = progressos[ultimo.inventario_id]
            progresso = (total.aprovados, total.esperados)
        linhas.append(
            LinhaEmpresa(
                empresa_id=empresa.empresa_id,
                empresa=empresa.nome,
                grupo=empresa.grupo,
                ano=None if ultimo is None else ultimo.ano,
                estado=None if ultimo is None else ultimo.estado,
                coleta=progresso,
                escopo1=None if ultimo is None else ultimo.escopo1_tco2e,
                escopo2=None if ultimo is None else ultimo.escopo2_tco2e,
                escopo3=None if ultimo is None else ultimo.escopo3_tco2e,
                preliminar=ultimo is not None and ultimo.estado != "fechado",
            )
        )

    anos: dict[int, list[Any]] = defaultdict(list)
    for numero in numeros:
        if numero.escopo1_tco2e is not None:
            anos[numero.ano].append(numero)
    serie = [
        AnoConsolidado(
            ano=ano,
            escopo1=sum((n.escopo1_tco2e for n in itens), Decimal(0)),
            escopo2=sum((n.escopo2_tco2e for n in itens), Decimal(0)),
            escopo3=sum((n.escopo3_tco2e for n in itens), Decimal(0)),
            empresas=len(itens),
            preliminar=any(n.estado != "fechado" for n in itens),
        )
        for ano, itens in sorted(anos.items())
    ]
    return linhas, serie
