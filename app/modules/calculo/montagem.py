"""Montagem da entrada do metodo: fontes, lancamentos aprovados e referencia -> Entrada.

A mesma montagem serve ao calculo no banco (referencia lida das tabelas de fatores) e a
paridade com a ferramenta oficial (referencia lida dos arquivos do caso em data/paridade).
Assim a paridade testa o codigo que roda de verdade, nao uma copia.

Regras:
- entram fontes ativas, materiais e dos escopos 1, 2 e 3, de unidades dentro do limite
  organizacional; unidade a 0% na abordagem do inventario fica fora, sem pendencia (1.3.0);
- entra so o dado aprovado (versao mais recente com decisao aprovada); o resto e pendencia;
- data de referencia: no escopo 2, sempre o ultimo dia do periodo (o fator do SIN e do mes
  do consumo); nos escopos 1 e 3, a data da compra e, sem ela, o ultimo dia do periodo;
- mistura pela data de referencia; componente sem fator deixa o lancamento inteiro pendente,
  para nao subestimar.
"""

import calendar
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from app.modules.calculo.fator_direto import (
    Componente,
    Entrada,
    FatorUsado,
    GwpUsado,
    Item,
    Pendencia,
)
from app.modules.organizacao.limites import Abordagem, FatosDaUnidade, percentual_consolidado

PREFIXOS_CALCULADOS = ("escopo1.", "escopo2.", "escopo3.")


@dataclass(frozen=True)
class ParteDaMistura:
    componente: str
    percentual: Decimal
    mistura_versao_id: UUID | None
    hash_conteudo: str


class Referencia(Protocol):
    """Fatores, misturas e GWP como conhecidos no instante do calculo."""

    async def mistura(self, combustivel: str, quando: date) -> list[ParteDaMistura]: ...

    async def fatores(self, categoria: str, codigo: str, quando: date) -> list[FatorUsado]: ...

    async def gwp(self, conjunto: str, gas: str) -> GwpUsado | None: ...


@dataclass(frozen=True)
class FonteParaCalculo:
    fonte_id: UUID
    categoria: str
    atividade: str | None
    periodicidade: str
    participacao_societaria: Decimal
    controle_operacional: bool
    controle_financeiro: bool


@dataclass(frozen=True)
class DadoAtual:
    """Versao mais recente de um periodo; decisao None e aguardando aprovacao."""

    lancamento_id: UUID
    quantidade: Decimal
    unidade: str
    data_compra: date | None
    decisao: str | None


def periodos(periodicidade: str) -> tuple[int, ...]:
    return tuple(range(1, 13)) if periodicidade == "mensal" else (0,)


def fim_do_periodo(ano: int, periodo: int) -> date:
    mes = 12 if periodo == 0 else periodo
    return date(ano, mes, calendar.monthrange(ano, mes)[1])


def data_de_referencia(
    ano: int, periodo: int, data_compra: date | None, categoria: str = "escopo1."
) -> date:
    if categoria.startswith("escopo2.") or data_compra is None:
        return fim_do_periodo(ano, periodo)
    return data_compra


def entra_no_calculo(categoria: str) -> bool:
    return categoria.startswith(PREFIXOS_CALCULADOS)


async def montar(
    *,
    ano: int,
    abordagem: Abordagem,
    gwp_conjunto: str,
    conhecido_em: datetime,
    fontes: list[FonteParaCalculo],
    atuais: dict[tuple[UUID, int], DadoAtual],
    referencia: Referencia,
) -> Entrada:
    pendencias: list[Pendencia] = []
    itens: list[Item] = []
    em_cache: dict[tuple[str, str, date], tuple[Componente, ...] | None] = {}

    async def componentes(
        categoria: str, atividade: str, quando: date
    ) -> tuple[Componente, ...] | None:
        chave = (categoria, atividade, quando)
        if chave in em_cache:
            return em_cache[chave]
        mistura = await referencia.mistura(atividade, quando)
        partes: list[tuple[str, Decimal | None, UUID | None, str | None]] = (
            [(m.componente, m.percentual, m.mistura_versao_id, m.hash_conteudo) for m in mistura]
            if mistura
            else [(atividade, None, None, None)]
        )
        montados: list[Componente] = []
        for codigo, percentual, mistura_id, hash_mistura in partes:
            fatores = await referencia.fatores(categoria, codigo, quando)
            if not fatores:
                em_cache[chave] = None
                return None
            montados.append(
                Componente(codigo, percentual, mistura_id, hash_mistura, tuple(fatores))
            )
        em_cache[chave] = tuple(montados)
        return em_cache[chave]

    for fonte in sorted(fontes, key=lambda f: str(f.fonte_id)):
        if not entra_no_calculo(fonte.categoria):
            continue
        consolidado = percentual_consolidado(
            abordagem,
            FatosDaUnidade(
                fonte.participacao_societaria,
                fonte.controle_operacional,
                fonte.controle_financeiro,
            ),
        )
        if consolidado == 0:
            continue
        if not fonte.atividade:
            pendencias.append(
                Pendencia(
                    fonte.fonte_id, None, "sem_atividade", "Escolha o combustível ou a atividade."
                )
            )
            continue
        for periodo in periodos(fonte.periodicidade):
            atual = atuais.get((fonte.fonte_id, periodo))
            if atual is None or atual.decisao != "aprovado":
                situacao = (
                    "sem dado"
                    if atual is None
                    else "devolvido"
                    if atual.decisao == "devolvido"
                    else "aguardando aprovação"
                )
                pendencias.append(Pendencia(fonte.fonte_id, periodo, "falta_dado", situacao))
                continue
            quando = data_de_referencia(ano, periodo, atual.data_compra, fonte.categoria)
            achados = await componentes(fonte.categoria, fonte.atividade, quando)
            if achados is None:
                pendencias.append(
                    Pendencia(
                        fonte.fonte_id,
                        periodo,
                        "sem_fator",
                        f"{fonte.atividade} em {quando.strftime('%d/%m/%Y')}",
                    )
                )
                continue
            itens.append(
                Item(
                    fonte_id=fonte.fonte_id,
                    lancamento_id=atual.lancamento_id,
                    periodo=periodo,
                    categoria=fonte.categoria,
                    atividade=fonte.atividade,
                    quantidade=atual.quantidade,
                    unidade=atual.unidade,
                    data_referencia=quando,
                    percentual_consolidado=consolidado,
                    componentes=achados,
                )
            )

    gases = sorted(
        {f.gas for i in itens for c in i.componentes for f in c.fatores if not f.biogenico}
    )
    gwps: dict[str, GwpUsado] = {}
    for gas in gases:
        achado = await referencia.gwp(gwp_conjunto, gas)
        if achado is not None:
            gwps[gas] = achado
    return Entrada(
        gwp_conjunto=gwp_conjunto,
        conhecido_em=conhecido_em,
        itens=tuple(itens),
        gwps=gwps,
        pendencias=tuple(pendencias),
    )
