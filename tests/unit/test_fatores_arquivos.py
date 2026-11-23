"""Leitura dos arquivos de referencia, com os dados ficticios de tests/dados_ficticios."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.modules.fatores.arquivos import (
    ErroArquivo,
    hash_conteudo,
    ler_fatores,
    ler_gwp,
    ler_misturas,
)

FICTICIOS = Path(__file__).resolve().parents[1] / "dados_ficticios"


def _bytes(texto: str) -> bytes:
    return texto.encode("utf-8")


def _linhas_com_erro(erro: ErroArquivo) -> dict[int, str]:
    return {p.linha: p.coluna for p in erro.problemas}


def test_gwp_ficticio() -> None:
    linhas = ler_gwp((FICTICIOS / "gwp.csv").read_bytes())
    assert [(r.gas, r.valor) for r in linhas] == [
        ("CO2", Decimal(1)),
        ("CH4", Decimal("11.5")),
        ("N2O", Decimal(222)),
    ]


def test_fatores_ficticios() -> None:
    linhas = ler_fatores((FICTICIOS / "fatores.csv").read_bytes())
    assert len(linhas) == 5
    ch4 = next(r for r in linhas if r.gas == "CH4")
    assert ch4.valor == Decimal("0.00015")  # notacao 1,5E-04
    assert ch4.vigencia_inicio == date(2020, 1, 1)  # data ISO tambem vale
    assert ch4.vigencia_fim is None
    assert any(r.biogenico for r in linhas)
    assert next(r for r in linhas if r.codigo == "teste.energia_y").referencia == "doc-teste"


def test_misturas_ficticias() -> None:
    linhas = ler_misturas((FICTICIOS / "misturas.csv").read_bytes())
    assert len(linhas) == 4


def test_bom_do_excel_e_aceito() -> None:
    conteudo = b"\xef\xbb\xbf" + (FICTICIOS / "gwp.csv").read_bytes()
    assert len(ler_gwp(conteudo)) == 3


def test_todos_os_erros_saem_juntos_com_linha_e_coluna() -> None:
    texto = (
        "codigo;descricao;categoria;gas;biogenico;valor;unidade;vigencia_inicio;vigencia_fim;fonte;referencia\n"
        "Teste.X;d;escopo1.a;CO2;nao;1,0;kg/L;01/01/2020;;f;\n"  # 2: codigo com maiuscula
        "teste.x;d;escopo1.a;CO2;talvez;1.5;L/kg;2020/01/01;;;\n"  # 3: cinco erros
        "teste.y;d;escopo1.a;CO2;nao;-1;kg/L;01/01/2020;31/12/2019;f;\n"  # 4: negativo
        "teste.z;d;escopo1.a;CO2;nao;1;kg/L;01/01/2020;31/12/2019;f;\n"  # 5: fim antes do inicio
        "teste.w;d;escopo1.a;CO2;nao;1;kg/L;01/01/2020;;f\n"  # 6: colunas a menos
    )
    with pytest.raises(ErroArquivo) as erro:
        ler_fatores(_bytes(texto))
    problemas = erro.value.problemas
    por_linha: dict[int, set[str]] = {}
    for p in problemas:
        por_linha.setdefault(p.linha, set()).add(p.coluna)
    assert por_linha[2] == {"codigo"}
    assert por_linha[3] == {"biogenico", "valor", "unidade", "vigencia_inicio", "fonte"}
    assert por_linha[4] == {"valor"}
    assert por_linha[5] == {"vigencia_fim"}
    assert por_linha[6] == {"-"}
    assert "linha 3, coluna valor" in str(erro.value)


def test_vigencia_sobreposta_no_mesmo_arquivo() -> None:
    texto = (
        "codigo;descricao;categoria;gas;biogenico;valor;unidade;vigencia_inicio;vigencia_fim;fonte;referencia\n"
        "teste.x;d;escopo1.a;CO2;nao;1;kg/L;01/01/2020;31/12/2025;f;\n"
        "teste.x;d;escopo1.a;CO2;nao;2;kg/L;01/06/2025;;f;\n"
        "teste.x;d;escopo1.a;CO2;sim;2;kg/L;01/06/2025;;f;\n"  # biogenico: outra chave, ok
        "teste.x;d;escopo1.b;CO2;nao;3;kg/L;01/06/2025;;f;\n"  # outra categoria: outra chave
    )
    with pytest.raises(ErroArquivo) as erro:
        ler_fatores(_bytes(texto))
    assert _linhas_com_erro(erro.value) == {3: "vigencia_inicio"}


def test_mistura_que_passa_de_100() -> None:
    texto = (
        "combustivel;componente;percentual;vigencia_inicio;vigencia_fim;fonte\n"
        "teste.d;teste.a;60;01/01/2020;;f\n"
        "teste.d;teste.b;50;01/01/2020;;f\n"
    )
    with pytest.raises(ErroArquivo) as erro:
        ler_misturas(_bytes(texto))
    assert _linhas_com_erro(erro.value) == {3: "percentual"}


@pytest.mark.parametrize(
    ("texto", "coluna"),
    [
        ("conjunto;gas;valor\nTESTE;CO2;1\n", "cabecalho"),
        ("conjunto;gas;valor;fonte;extra\nTESTE;CO2;1;f;x\n", "cabecalho"),
        ("", "cabecalho"),
    ],
)
def test_cabecalho_errado(texto: str, coluna: str) -> None:
    with pytest.raises(ErroArquivo) as erro:
        ler_gwp(_bytes(texto))
    assert erro.value.problemas[0].coluna == coluna


def test_gwp_repetido_e_zero() -> None:
    texto = "conjunto;gas;valor;fonte\nTESTE;CO2;1;f\nTESTE;CO2;1;f\nTESTE;CH4;0;f\n"
    with pytest.raises(ErroArquivo) as erro:
        ler_gwp(_bytes(texto))
    assert _linhas_com_erro(erro.value) == {3: "gas", 4: "valor"}


def test_arquivo_fora_de_utf8() -> None:
    with pytest.raises(ErroArquivo):
        ler_gwp("conjunto;gas;valor;fonte\nTESTE;CO2;1;São Paulo\n".encode("latin-1"))


def test_hash_ignora_escala_mas_nao_o_valor_nem_a_fonte() -> None:
    base = ler_gwp((FICTICIOS / "gwp.csv").read_bytes())[1]
    assert hash_conteudo("gwp", base) == hash_conteudo("gwp", replace(base, valor=Decimal("11.50")))
    assert hash_conteudo("gwp", base) != hash_conteudo("gwp", replace(base, valor=Decimal("11.6")))
    assert hash_conteudo("gwp", base) != hash_conteudo("gwp", replace(base, fonte="outra"))
    assert hash_conteudo("gwp", base) != hash_conteudo("fatores", base)
