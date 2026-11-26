"""Paridade com a ferramenta oficial: cada caso de data/paridade precisa bater na tolerancia.

Os casos oficiais (calculados na ferramenta do Programa) moram em data/paridade/<caso>/.
Enquanto nao houver nenhum, o teste deles fica pulado com o motivo, e o caso FICTICIO de
tests/dados_ficticios/paridade prova que o comparador funciona e sabe falhar.

Caso ficticio, feito a mao:
  caldeira: 1.000 L de diesel comercial (85/15), 100% consolidado
    fossil 850 L: CO2 2,21 t; CH4 0,34 kg x 28 = 0,00952 tCO2e
    bio    150 L: CO2 biogenico 0,36 t; CH4 0,045 kg x 28 = 0,00126 tCO2e
    escopo 1 = 2,22078 tCO2e
  energia: janeiro 100 MWh x 0,05 t/MWh + fevereiro 200 MWh x 0,1 t/MWh = 25 tCO2e
    (o fator de cada mes vale so no mes: a data de referencia e o fim do mes do consumo)
"""

import asyncio
import shutil
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.config import obter_configuracao
from app.modules.calculo import paridade

RAIZ = Path(__file__).resolve().parents[2]
OFICIAIS = paridade.pastas_de_casos(RAIZ / "data" / "paridade")
FICTICIOS = paridade.pastas_de_casos(RAIZ / "tests" / "dados_ficticios" / "paridade")


def _tolerancia() -> Decimal:
    return obter_configuracao().paridade_tolerancia_tco2e


def _rodar(pasta: Path) -> paridade.Caso:
    return asyncio.run(paridade.carregar_caso(pasta))


@pytest.mark.skipif(not OFICIAIS, reason="nenhum caso oficial em data/paridade ainda")
@pytest.mark.parametrize("pasta", OFICIAIS, ids=[p.name for p in OFICIAIS])
def test_caso_oficial_bate_com_a_ferramenta(pasta: Path) -> None:
    caso = _rodar(pasta)
    # Pendencia tira o lancamento da conta: o total pode caber na tolerancia sem bater de fato.
    assert caso.resultado.pendencias == (), f"{caso.nome}: o caso tem pendencias: " + "; ".join(
        f"{p.motivo} {p.detalhe}" for p in caso.resultado.pendencias
    )
    divergencias = paridade.comparar(caso, _tolerancia())
    assert not divergencias, (
        f"{caso.nome} ({caso.ferramenta}, {paridade.hash_do_caso(pasta)[:12]}):\n"
        + "\n".join(str(d) for d in divergencias)
    )


@pytest.mark.parametrize("pasta", FICTICIOS, ids=[p.name for p in FICTICIOS])
def test_caso_ficticio_bate(pasta: Path) -> None:
    caso = _rodar(pasta)
    assert paridade.comparar(caso, _tolerancia()) == []
    assert caso.resultado.pendencias == ()


def test_comparador_sabe_falhar() -> None:
    caso = _rodar(FICTICIOS[0])
    errado = replace(caso, esperado={**caso.esperado, "escopo2_tco2e": Decimal("25.002")})
    divergencias = paridade.comparar(errado, _tolerancia())
    assert [d.indicador for d in divergencias] == ["escopo2_tco2e"]
    # Dentro da tolerancia passa; um milesimo acima, nao.
    no_limite = replace(caso, esperado={**caso.esperado, "escopo2_tco2e": Decimal("25.001")})
    assert paridade.comparar(no_limite, Decimal("0.001")) == []


def test_tolerancia_vem_da_configuracao() -> None:
    assert _tolerancia() == Decimal("0.001")


def test_caso_incompleto_diz_o_que_falta(tmp_path: Path) -> None:
    copia = tmp_path / "caso_incompleto"
    shutil.copytree(FICTICIOS[0], copia)
    (copia / "esperado.csv").unlink()
    with pytest.raises(paridade.CasoInvalido, match=r"falta o arquivo esperado.csv"):
        _rodar(copia)
