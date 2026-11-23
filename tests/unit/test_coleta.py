"""Coleta sem banco: arquivos, CSV seguro, ClamAV, validacoes e planilha."""

import asyncio
import io
import re
import struct
import zipfile
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.antivirus import AntivirusIndisponivel, ArquivoInfectado, ClienteClamav
from app.core.arquivos import (
    CSV,
    PDF,
    XLSX,
    ArquivoRecusado,
    conferir_arquivo,
    nome_seguro,
)
from app.core.csv_seguro import celula_segura, gerar_csv
from app.modules.coleta import planilha, servico
from app.modules.coleta.categorias import CATEGORIAS, periodos, rotulo_periodo
from app.web.formatos import formatar_numero_exato, ler_numero

LIMITE = 1024 * 1024
HOJE = date(2026, 3, 10)


def _zip(arquivos: dict[str, bytes]) -> bytes:
    saida = io.BytesIO()
    with zipfile.ZipFile(saida, "w") as pacote:
        for nome, conteudo in arquivos.items():
            pacote.writestr(nome, conteudo)
    return saida.getvalue()


XLSX_MINIMO = _zip({"[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b"<workbook/>"})


# ---------------------------------------------------------------- arquivos


def test_tipo_vem_do_conteudo() -> None:
    assert conferir_arquivo("nota.pdf", b"%PDF-1.7 ...", limite_bytes=LIMITE) == ("nota.pdf", PDF)
    assert conferir_arquivo("dados.xlsx", XLSX_MINIMO, limite_bytes=LIMITE)[1] is XLSX
    assert conferir_arquivo("coleta.csv", b"a;b\n1;2\n", limite_bytes=LIMITE)[1] is CSV


@pytest.mark.parametrize(
    ("nome", "conteudo", "trecho"),
    [
        ("macro.xlsm", XLSX_MINIMO, "macro"),
        ("MACRO.XLSB", b"qualquer", "macro"),
        (
            "disfarce.xlsx",
            _zip(
                {**{"[Content_Types].xml": b"", "xl/workbook.xml": b""}, "xl/vbaProject.bin": b""}
            ),
            "macro",
        ),
        ("pacote.zip", _zip({"a.txt": b"oi"}), "compactados"),
        ("antigo.xls", b"\xd0\xcf\x11\xe0" + bytes(20), "Formato antigo"),
        ("vazio.pdf", b"", "vazio"),
        ("grande.pdf", b"%PDF-" + bytes(LIMITE), "passa de 1 MB"),
        ("nota.pdf", b"\x89PNG\r\n\x1a\n" + bytes(10), "extensão .png"),
        ("programa.exe", b"MZ\x90\x00" + bytes(10), "não aceito"),
        ("binario.txt", b"\x00\x01\x02", "não aceito"),
        ("", b"%PDF-", "precisa ter nome"),
    ],
)
def test_arquivos_recusados(nome: str, conteudo: bytes, trecho: str) -> None:
    with pytest.raises(ArquivoRecusado, match=trecho):
        conferir_arquivo(nome, conteudo, limite_bytes=LIMITE)


def test_csv_do_excel_em_windows_1252_e_aceito() -> None:
    conteudo = "descrição;quantidade\nCaldeira;10\n".encode("cp1252")
    assert conferir_arquivo("coleta.csv", conteudo, limite_bytes=LIMITE)[1] is CSV


@pytest.mark.parametrize(
    ("bruto", "esperado"),
    [
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("C:\\Users\\fulano\\nota fiscal.pdf", "nota fiscal.pdf"),
        ("nota\x00\x1f<>.pdf", "nota.pdf"),
        ("a" * 300 + ".pdf", "a" * 196 + ".pdf"),
    ],
)
def test_nome_seguro(bruto: str, esperado: str) -> None:
    assert nome_seguro(bruto) == esperado


# ---------------------------------------------------------------- CSV seguro


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        ('=HYPERLINK("x")', '\'=HYPERLINK("x")'),
        ("+1", "'+1"),
        ("-2+3", "'-2+3"),
        ("@SOMA(A1)", "'@SOMA(A1)"),
        ("\tcmd", "'\tcmd"),
        ("Caldeira 1", "Caldeira 1"),
        ("1.234,5", "1.234,5"),
        (None, ""),
    ],
)
def test_celula_segura(valor: object, esperado: str) -> None:
    assert celula_segura(valor) == esperado


def test_gerar_csv_com_bom_e_ponto_e_virgula() -> None:
    conteudo = gerar_csv(["a", "b"], [["=1", "x;y"]])
    assert conteudo.startswith("\ufeff".encode())
    assert conteudo.decode("utf-8-sig") == 'a;b\r\n\'=1;"x;y"\r\n'


# ---------------------------------------------------------------- ClamAV


@pytest.fixture
async def clamd_falso() -> AsyncIterator[tuple[int, list[bytes]]]:
    """clamd minimo: le o INSTREAM e responde FOUND para a assinatura EICAR."""
    recebidos: list[bytes] = []

    async def atender(leitor: asyncio.StreamReader, escritor: asyncio.StreamWriter) -> None:
        comando = await leitor.readuntil(b"\0")
        assert comando == b"zINSTREAM\0"
        corpo = b""
        while True:
            tamanho = struct.unpack("!I", await leitor.readexactly(4))[0]
            if tamanho == 0:
                break
            corpo += await leitor.readexactly(tamanho)
        recebidos.append(corpo)
        if b"EICAR" in corpo:
            escritor.write(b"stream: Eicar-Test-Signature FOUND\0")
        elif b"QUEBRADO" in corpo:
            escritor.write(b"INSTREAM size limit exceeded. ERROR\0")
        else:
            escritor.write(b"stream: OK\0")
        await escritor.drain()
        escritor.close()

    servidor = await asyncio.start_server(atender, "127.0.0.1", 0)
    porta = servidor.sockets[0].getsockname()[1]
    async with servidor:
        yield porta, recebidos


async def test_clamav_limpo_infectado_e_erro(clamd_falso: tuple[int, list[bytes]]) -> None:
    porta, recebidos = clamd_falso
    cliente = ClienteClamav("127.0.0.1", porta, 5)
    grande = b"x" * (200 * 1024)
    await cliente.verificar(grande)
    assert recebidos[-1] == grande  # enviado em blocos e remontado inteiro
    with pytest.raises(ArquivoInfectado):
        await cliente.verificar(b"X5O!P%@AP EICAR")
    with pytest.raises(AntivirusIndisponivel):
        await cliente.verificar(b"QUEBRADO")


async def test_clamav_fora_do_ar_recusa() -> None:
    servidor = await asyncio.start_server(lambda _l, _e: None, "127.0.0.1", 0)
    porta = servidor.sockets[0].getsockname()[1]
    servidor.close()
    await servidor.wait_closed()
    with pytest.raises(AntivirusIndisponivel):
        await ClienteClamav("127.0.0.1", porta, 2).verificar(b"qualquer")


# ---------------------------------------------------------------- validacoes


@pytest.mark.parametrize(
    ("texto", "esperado", "erro"),
    [
        ("1.234,5", Decimal("1234.5"), None),
        ("0", Decimal(0), None),
        ("0,0015", Decimal("0.0015"), None),
        ("", None, "Informe a quantidade"),
        ("1.5", None, "vírgula"),
        ("-3", None, "negativa"),
        ("1,1234567", None, "6 casas"),
        ("1" + "0" * 15, None, "grande demais"),
    ],
)
def test_validar_quantidade(texto: str, esperado: Decimal | None, erro: str | None) -> None:
    valor, mensagem = servico.validar_quantidade(texto)
    assert valor == esperado
    assert (mensagem is None) if erro is None else (erro in (mensagem or ""))


@pytest.mark.parametrize(
    ("texto", "ano", "esperado", "erro"),
    [
        ("", 2025, None, None),
        ("15/03/2025", 2025, date(2025, 3, 15), None),
        ("2024-12-20", 2025, date(2024, 12, 20), None),
        ("31/02/2025", 2025, None, "dd/mm/aaaa"),
        ("01/01/2023", 2025, None, "2024 ou 2025"),
        ("11/03/2026", 2026, None, "futuro"),
    ],
)
def test_validar_data_compra(texto: str, ano: int, esperado: date | None, erro: str | None) -> None:
    valor, mensagem = servico.validar_data_compra(texto, ano, HOJE)
    assert valor == esperado
    assert (mensagem is None) if erro is None else (erro in (mensagem or ""))


@pytest.mark.parametrize(
    ("texto", "aceita"),
    [("L", True), ("m**3", True), ("kWh", True), ("t", True), ("GJ", True), ("litro", False),
     ("", False), ("percent", False)],
)  # fmt: skip
def test_validar_unidade_medida(texto: str, aceita: bool) -> None:
    assert (servico.validar_unidade_medida(texto)[1] is None) is aceita


@pytest.mark.parametrize(
    ("texto", "erro"), [("2025", None), ("1999", "entre"), ("2027", "entre"), ("25", "quatro")]
)
def test_validar_ano(texto: str, erro: str | None) -> None:
    mensagem = servico.validar_ano(texto, HOJE)[1]
    assert (mensagem is None) if erro is None else (erro in (mensagem or ""))


# ---------------------------------------------------------------- categorias e planilha


def test_categorias_no_formato_dos_fatores() -> None:
    codigo = re.compile(r"^escopo[12]\.[a-z0-9_]+$")
    assert all(codigo.fullmatch(c.codigo) for c in CATEGORIAS)
    assert len({c.codigo for c in CATEGORIAS}) == len(CATEGORIAS)
    eletrica = next(c for c in CATEGORIAS if c.codigo == "escopo2.energia_eletrica")
    assert eletrica.exige_mensal


def test_periodos() -> None:
    assert periodos("mensal") == tuple(range(1, 13))
    assert periodos("anual") == (0,)
    assert rotulo_periodo(0) == "Ano inteiro"
    assert rotulo_periodo(3) == "Março"


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [("ano", 0), ("ANO", 0), ("01", 1), ("12", 12), ("13", None), ("0", None), ("x", None)],
)
def test_ler_periodo(texto: str, esperado: int | None) -> None:
    assert planilha._ler_periodo(texto) == esperado


@given(st.decimals(min_value=Decimal(0), max_value=Decimal("999999999"), places=6, allow_nan=False))
def test_quantidade_da_planilha_volta_igual(valor: Decimal) -> None:
    """O que o modelo escreve, a importacao le de volta sem perder nada."""
    assert ler_numero(planilha._quantidade_texto(valor)) == valor


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        (Decimal("0.0015"), "0,0015"),
        (Decimal("1234.5"), "1.234,50"),
        (Decimal("10"), "10,00"),
        (Decimal("0.12345678"), "0,123457"),
        (None, ""),
    ],
)
def test_formatar_numero_exato(valor: Decimal | None, esperado: str) -> None:
    assert formatar_numero_exato(valor) == esperado
