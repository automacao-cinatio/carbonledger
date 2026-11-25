"""Jornada do inventario na tela inicial: onde a empresa em foco esta e o proximo passo.

Etapas na ordem do trabalho: organizacao, coleta, resultados, fechamento, relatorio assinado e
verificacao. A primeira etapa nao concluida e o proximo passo. Tudo lido com o RLS de quem
esta logado.
"""

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class Etapa:
    nome: str
    url: str
    feita: bool
    detalhe: str


@dataclass(frozen=True)
class Jornada:
    etapas: list[Etapa]
    escopo1: Decimal | None
    escopo2: Decimal | None
    escopo3: Decimal | None = None

    @property
    def proxima(self) -> Etapa | None:
        return next((e for e in self.etapas if not e.feita), None)


async def montar_jornada(sessao: AsyncSession, empresa_id: UUID) -> Jornada:
    unidades = await sessao.scalar(
        text("SELECT count(*) FROM unidade WHERE empresa_id = :e AND ativa"), {"e": empresa_id}
    )
    inventario = (
        await sessao.execute(
            text(
                "SELECT inventario_id, ano, estado FROM inventario WHERE empresa_id = :e"
                " ORDER BY ano DESC LIMIT 1"
            ),
            {"e": empresa_id},
        )
    ).first()
    calculo = None
    concessoes = 0
    relatorios = 0
    if inventario is not None:
        calculo = (
            await sessao.execute(
                text(
                    "SELECT escopo1_tco2e, escopo2_tco2e, escopo3_tco2e, pendencias FROM calculo"
                    " WHERE inventario_id = :i ORDER BY calculado_em DESC LIMIT 1"
                ),
                {"i": inventario.inventario_id},
            )
        ).first()
        concessoes = (
            await sessao.scalar(
                text(
                    "SELECT count(*) FROM concessao_verificacao WHERE inventario_id = :i"
                    " AND revogado_em IS NULL AND expira_em > now()"
                ),
                {"i": inventario.inventario_id},
            )
            or 0
        )
        relatorios = (
            await sessao.scalar(
                text("SELECT count(*) FROM relatorio_inventario WHERE inventario_id = :i"),
                {"i": inventario.inventario_id},
            )
            or 0
        )
    fechado = inventario is not None and inventario.estado == "fechado"
    if inventario is None:
        coleta = "Abra o inventário do ano."
    elif fechado:
        coleta = f"Inventário {inventario.ano} fechado."
    else:
        coleta = f"Inventário {inventario.ano} em coleta."
    etapas = [
        Etapa(
            "Organização",
            "/organizacao",
            bool(unidades),
            f"{unidades} unidade(s) ativa(s)." if unidades else "Cadastre as unidades da empresa.",
        ),
        Etapa(
            "Coleta",
            "/coleta",
            inventario is not None and (fechado or (calculo is not None)),
            coleta,
        ),
        Etapa(
            "Resultados",
            "/resultados",
            calculo is not None and calculo.pendencias == 0,
            "Sem cálculo ainda."
            if calculo is None
            else f"Último cálculo com {calculo.pendencias} pendência(s).",
        ),
        Etapa(
            "Fechamento",
            "/fechamento",
            fechado,
            "Inventário fechado." if fechado else "Feche o inventário sem pendências.",
        ),
        Etapa(
            "Relatório assinado",
            "/relatorios",
            bool(relatorios),
            "Relatório assinado gerado." if relatorios else "Gere o relatório assinado.",
        ),
        Etapa(
            "Verificação",
            "/verificacao",
            bool(concessoes),
            "Verificador com acesso." if concessoes else "Dê acesso ao verificador.",
        ),
    ]
    return Jornada(
        etapas,
        None if calculo is None else calculo.escopo1_tco2e,
        None if calculo is None else calculo.escopo2_tco2e,
        None if calculo is None else calculo.escopo3_tco2e,
    )
