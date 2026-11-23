"""Categorias de fonte de emissao dos escopos 1 e 2 (estrutura do GHG Protocol).

So a estrutura: nenhum fator, GWP ou percentual mora aqui. O codigo segue o formato da coluna
categoria de data/fatores (escopo1.combustao_estacionaria), para o calculo achar os fatores.
O escopo 3 entra no P17.

Energia eletrica e sempre mensal: o fator do SIN e mensal (P10).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Categoria:
    codigo: str
    escopo: int
    rotulo: str
    ajuda: str
    exige_mensal: bool = False


CATEGORIAS: tuple[Categoria, ...] = (
    Categoria(
        "escopo1.combustao_estacionaria",
        1,
        "Combustão estacionária",
        "Combustível queimado em equipamentos parados: caldeiras, fornos, geradores.",
    ),
    Categoria(
        "escopo1.combustao_movel",
        1,
        "Combustão móvel",
        "Combustível usado em veículos e máquinas da própria empresa.",
    ),
    Categoria(
        "escopo1.fugitivas",
        1,
        "Emissões fugitivas",
        "Vazamento de gases, como os de refrigeração, ar-condicionado e extintores.",
    ),
    Categoria(
        "escopo1.processos_industriais",
        1,
        "Processos industriais",
        "Gases liberados pelas reações do processo produtivo, fora a queima de combustível.",
    ),
    Categoria(
        "escopo1.atividades_agricolas",
        1,
        "Atividades agrícolas",
        "Criação de animais, manejo de dejetos, fertilizantes e calcário.",
    ),
    Categoria(
        "escopo1.mudanca_uso_solo",
        1,
        "Mudança no uso do solo",
        "Conversão de áreas de vegetação nativa em outros usos.",
    ),
    Categoria(
        "escopo1.residuos_solidos",
        1,
        "Resíduos sólidos",
        "Resíduos tratados pela própria empresa: aterro, compostagem, incineração.",
    ),
    Categoria(
        "escopo1.efluentes",
        1,
        "Efluentes",
        "Efluentes tratados pela própria empresa.",
    ),
    Categoria(
        "escopo2.energia_eletrica",
        2,
        "Energia elétrica comprada",
        "Eletricidade consumida da rede, como aparece na conta de luz.",
        exige_mensal=True,
    ),
    Categoria(
        "escopo2.energia_termica",
        2,
        "Energia térmica comprada",
        "Vapor, calor ou frio comprados de terceiros.",
    ),
    Categoria(
        "escopo2.perdas_transmissao",
        2,
        "Perdas na transmissão e distribuição",
        "Só para quem opera linhas de transmissão ou distribuição de energia.",
    ),
)

POR_CODIGO = {c.codigo: c for c in CATEGORIAS}


def categoria(codigo: str) -> Categoria | None:
    return POR_CODIGO.get(codigo)


def rotulo(codigo: str) -> str:
    achada = POR_CODIGO.get(codigo)
    return achada.rotulo if achada else codigo


MESES = (
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
)  # fmt: skip


def rotulo_periodo(periodo: int) -> str:
    return "Ano inteiro" if periodo == 0 else MESES[periodo - 1]


def periodos(periodicidade: str) -> tuple[int, ...]:
    return tuple(range(1, 13)) if periodicidade == "mensal" else (0,)
