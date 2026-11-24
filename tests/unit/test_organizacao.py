from decimal import Decimal

import pytest

from app.modules.organizacao.estrutura import validar_cnae, validar_cnpj, validar_participacao
from app.modules.organizacao.limites import Abordagem, FatosDaUnidade, percentual_consolidado
from app.modules.organizacao.validacao import (
    UFS,
    cnae_no_formato,
    cnpj_valido,
    formatar_cnpj,
    normalizar_cnpj,
)

# ---------------------------------------------------------------- CNPJ


@pytest.mark.parametrize(
    "cnpj",
    [
        "11222333000181",  # numerico classico
        "12ABC34501DE35",  # exemplo alfanumerico divulgado pela Receita Federal
    ],
)
def test_cnpj_valido(cnpj: str) -> None:
    assert cnpj_valido(cnpj)


@pytest.mark.parametrize(
    "cnpj",
    [
        "11222333000182",  # digito errado
        "12ABC34501DE36",
        "00000000000000",  # todos iguais
        "AAAAAAAAAAAAAA",
        "1122233300018",  # curto
        "112223330001811",  # longo
        "12ABC34501DEA5",  # letra no digito verificador
        "12abc34501de35",  # minusculas sem normalizar
    ],
)
def test_cnpj_invalido(cnpj: str) -> None:
    assert not cnpj_valido(cnpj)


def test_normalizar_e_formatar_cnpj() -> None:
    assert normalizar_cnpj(" 12.abc.345/01de-35 ") == "12ABC34501DE35"
    assert formatar_cnpj("12ABC34501DE35") == "12.ABC.345/01DE-35"
    assert formatar_cnpj(None) == ""


def test_validar_cnpj_diz_como_corrigir() -> None:
    assert validar_cnpj("11.222.333/0001-81", obrigatorio=True) == ("11222333000181", None)
    assert validar_cnpj("", obrigatorio=False) == (None, None)
    assert validar_cnpj("", obrigatorio=True)[1] == "Informe o CNPJ."
    _, erro = validar_cnpj("11.222.333/0001-82", obrigatorio=True)
    assert erro is not None and "dígitos finais" in erro


# ---------------------------------------------------------------- CNAE, UF, participacao


@pytest.mark.parametrize(
    ("cnae", "ok"), [("2512-8/00", True), ("25128/00", False), ("2512-8/0", False)]
)
def test_cnae_no_formato(cnae: str, ok: bool) -> None:
    assert cnae_no_formato(cnae) is ok
    assert (validar_cnae(cnae)[1] is None) is ok


def test_ufs() -> None:
    assert len(UFS) == 27 and len(set(UFS)) == 27
    assert "DF" in UFS and "TO" in UFS


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("100", Decimal("100")),
        ("51,5", Decimal("51.5")),
        ("0", Decimal("0")),
        ("33,3333", Decimal("33.3333")),
    ],
)
def test_participacao_valida(texto: str, esperado: Decimal) -> None:
    assert validar_participacao(texto) == (esperado, None)


@pytest.mark.parametrize("texto", ["", "-1", "100,01", "abc", "1.5", "33,33333"])
def test_participacao_invalida(texto: str) -> None:
    valor, erro = validar_participacao(texto)
    assert valor is None and erro


# ---------------------------------------------------------------- limites organizacionais


@pytest.mark.parametrize(
    ("fatos", "esperado"),
    [
        (
            FatosDaUnidade(Decimal("51.5"), True, False),
            {"op": 100, "fin": 0, "part": Decimal("51.5")},
        ),
        (FatosDaUnidade(Decimal("30"), False, True), {"op": 0, "fin": 100, "part": Decimal("30")}),
        (
            FatosDaUnidade(Decimal("100"), True, True),
            {"op": 100, "fin": 100, "part": Decimal("100")},
        ),
        (FatosDaUnidade(Decimal("0"), False, False), {"op": 0, "fin": 0, "part": Decimal("0")}),
    ],
)
def test_percentual_consolidado(fatos: FatosDaUnidade, esperado: dict[str, Decimal | int]) -> None:
    assert percentual_consolidado(Abordagem.CONTROLE_OPERACIONAL, fatos) == esperado["op"]
    assert percentual_consolidado(Abordagem.CONTROLE_FINANCEIRO, fatos) == esperado["fin"]
    assert percentual_consolidado(Abordagem.PARTICIPACAO_SOCIETARIA, fatos) == esperado["part"]
    assert isinstance(percentual_consolidado(Abordagem.CONTROLE_OPERACIONAL, fatos), Decimal)


def test_participacao_fora_da_faixa_e_recusada() -> None:
    with pytest.raises(ValueError):
        percentual_consolidado(
            Abordagem.PARTICIPACAO_SOCIETARIA, FatosDaUnidade(Decimal("101"), True, True)
        )
