"""Categorias de fonte de emissao dos escopos 1, 2 e 3 (estrutura do GHG Protocol).

So a estrutura: nenhum fator, GWP ou percentual mora aqui. O codigo segue o formato da coluna
categoria de data/fatores (escopo1.combustao_estacionaria), para o calculo achar os fatores.
O escopo 3 segue as 15 categorias do GHG Protocol (Corporate Value Chain Standard).

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

_ESCOPO3 = (
    ("cat01_bens_servicos", "Bens e serviços comprados",
     "Insumos, materiais e serviços comprados."),
    ("cat02_bens_capital", "Bens de capital",
     "Máquinas, equipamentos, prédios e veículos comprados."),
    ("cat03_combustivel_energia", "Atividades de combustível e energia",
     "Extração e transporte dos combustíveis e da energia comprados, fora dos escopos 1 e 2."),
    ("cat04_transporte_upstream", "Transporte e distribuição (upstream)",
     "Frete contratado para trazer o que a empresa compra."),
    ("cat05_residuos", "Resíduos gerados nas operações",
     "Resíduos e efluentes tratados por terceiros."),
    ("cat06_viagens", "Viagens a negócios", "Voos, hospedagem e deslocamentos a trabalho."),
    ("cat07_deslocamento", "Deslocamento de funcionários", "Ida e volta do trabalho."),
    ("cat08_arrendados_upstream", "Bens arrendados (upstream)",
     "Bens alugados pela empresa e fora dos escopos 1 e 2."),
    ("cat09_transporte_downstream", "Transporte e distribuição (downstream)",
     "Frete dos produtos vendidos, não pago pela empresa."),
    ("cat10_processamento", "Processamento de produtos vendidos",
     "Transformação dos produtos vendidos por terceiros."),
    ("cat11_uso", "Uso de bens vendidos", "Emissões no uso dos produtos vendidos."),
    ("cat12_fim_de_vida", "Fim de vida dos produtos vendidos",
     "Descarte e tratamento dos produtos vendidos."),
    ("cat13_arrendados_downstream", "Bens arrendados (downstream)",
     "Bens da empresa alugados a terceiros."),
    ("cat14_franquias", "Franquias", "Operação das franquias."),
    ("cat15_investimentos", "Investimentos", "Emissões das empresas investidas."),
)  # fmt: skip
CATEGORIAS = CATEGORIAS + tuple(
    Categoria(f"escopo3.{codigo}", 3, f"{indice}. {rotulo_categoria}", ajuda)
    for indice, (codigo, rotulo_categoria, ajuda) in enumerate(_ESCOPO3, start=1)
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
