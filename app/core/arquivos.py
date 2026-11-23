"""Arquivos enviados: limite de tamanho, tipo pelo conteudo e recusa de macro.

A extensao do nome so serve para conferir: o tipo vem dos bytes. Arquivo com macro (.xlsm,
.docm, vbaProject.bin dentro do pacote) e recusado, assim como tipo que nao esta na lista.
O antivirus (app/core/antivirus.py) roda depois desta checagem e antes de guardar.
"""

import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

# Extensoes recusadas de cara, mesmo que o conteudo parecesse aceitavel.
EXTENSOES_COM_MACRO = frozenset({".xlsm", ".xltm", ".xlsb", ".xlam", ".docm", ".dotm", ".pptm"})
TAMANHO_MAXIMO_NOME = 200
# Limite de entradas no pacote: um zip com milhares de arquivos nao e planilha de verdade.
MAXIMO_ENTRADAS_ZIP = 5000


@dataclass(frozen=True)
class TipoArquivo:
    tipo_conteudo: str
    extensoes: frozenset[str]
    descricao: str


PDF = TipoArquivo("application/pdf", frozenset({".pdf"}), "PDF")
PNG = TipoArquivo("image/png", frozenset({".png"}), "imagem PNG")
JPEG = TipoArquivo("image/jpeg", frozenset({".jpg", ".jpeg"}), "imagem JPEG")
XLSX = TipoArquivo(
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    frozenset({".xlsx"}),
    "planilha do Excel",
)
DOCX = TipoArquivo(
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    frozenset({".docx"}),
    "documento do Word",
)
CSV = TipoArquivo("text/csv", frozenset({".csv"}), "CSV")
TXT = TipoArquivo("text/plain", frozenset({".txt"}), "texto")
XML = TipoArquivo("application/xml", frozenset({".xml"}), "XML")

TIPOS_EVIDENCIA = (PDF, PNG, JPEG, XLSX, DOCX, CSV, TXT, XML)
DESCRICAO_TIPOS_EVIDENCIA = "PDF, PNG, JPEG, XLSX, DOCX, CSV, TXT ou XML"


class ArquivoRecusado(ValueError):
    """Arquivo fora das regras. A mensagem diz como corrigir e vai para a tela."""


def nome_seguro(nome: str | None) -> str:
    """So o nome final (sem pasta), sem caracteres de controle, com no maximo 200 caracteres."""
    base = PureWindowsPath(PurePosixPath(nome or "").name).name
    limpo = "".join(c for c in base if c.isprintable() and c not in '<>:"/\\|?*').strip(" .")
    if not limpo:
        raise ArquivoRecusado("O arquivo precisa ter nome. Renomeie e envie de novo.")
    if len(limpo) > TAMANHO_MAXIMO_NOME:
        raiz = PurePosixPath(limpo)
        limpo = raiz.stem[: TAMANHO_MAXIMO_NOME - len(raiz.suffix)] + raiz.suffix
    return limpo


def extensao(nome: str) -> str:
    return PurePosixPath(nome).suffix.lower()


def _pacote_office(conteudo: bytes) -> TipoArquivo:
    try:
        with zipfile.ZipFile(io.BytesIO(conteudo)) as pacote:
            nomes = pacote.namelist()
    except (zipfile.BadZipFile, ValueError, OSError) as erro:
        raise ArquivoRecusado("O arquivo compactado está corrompido. Gere-o de novo.") from erro
    if len(nomes) > MAXIMO_ENTRADAS_ZIP:
        raise ArquivoRecusado("O pacote tem arquivos demais para ser um documento do Office.")
    minusculos = {n.lower() for n in nomes}
    if any(n.endswith("vbaproject.bin") for n in minusculos):
        raise ArquivoRecusado(
            "Arquivos com macro não são aceitos. Salve como .xlsx ou .docx, sem macros."
        )
    if "[content_types].xml" in minusculos and "xl/workbook.xml" in minusculos:
        return XLSX
    if "[content_types].xml" in minusculos and "word/document.xml" in minusculos:
        return DOCX
    raise ArquivoRecusado(
        "Arquivos compactados (.zip) não são aceitos. Envie cada documento separado."
    )


def _texto(conteudo: bytes, ext: str) -> TipoArquivo | None:
    if b"\x00" in conteudo:
        return None
    try:
        texto = conteudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        # O Excel em pt-BR salva "CSV (separado por virgulas)" em Windows-1252.
        if ext not in (".csv", ".txt"):
            return None
        try:
            texto = conteudo.decode("cp1252")
        except UnicodeDecodeError:
            return None
    inicio = texto.lstrip()[:5].lower()
    if inicio.startswith("<?xml") or (inicio.startswith("<") and ext == ".xml"):
        return XML
    return CSV if ext == ".csv" else TXT


def detectar_tipo(conteudo: bytes, ext: str) -> TipoArquivo | None:
    """Tipo pelos primeiros bytes (e, para Office, pela estrutura do pacote)."""
    if conteudo.startswith(b"%PDF-"):
        return PDF
    if conteudo.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if conteudo.startswith(b"\xff\xd8\xff"):
        return JPEG
    if conteudo.startswith(b"PK\x03\x04"):
        return _pacote_office(conteudo)
    if conteudo.startswith(b"\xd0\xcf\x11\xe0"):
        raise ArquivoRecusado(
            "Formato antigo do Office (.xls ou .doc) não é aceito. Salve como .xlsx ou .docx."
        )
    return _texto(conteudo, ext)


def conferir_arquivo(
    nome: str | None,
    conteudo: bytes,
    *,
    limite_bytes: int,
    aceitos: tuple[TipoArquivo, ...] = TIPOS_EVIDENCIA,
    descricao_aceitos: str = DESCRICAO_TIPOS_EVIDENCIA,
) -> tuple[str, TipoArquivo]:
    """Confere nome, tamanho e tipo. Devolve (nome seguro, tipo detectado)."""
    limpo = nome_seguro(nome)
    ext = extensao(limpo)
    if ext in EXTENSOES_COM_MACRO:
        raise ArquivoRecusado(
            "Arquivos com macro não são aceitos. Salve como .xlsx ou .docx, sem macros."
        )
    if not conteudo:
        raise ArquivoRecusado("O arquivo está vazio. Confira e envie de novo.")
    if len(conteudo) > limite_bytes:
        limite_mb = limite_bytes // (1024 * 1024)
        raise ArquivoRecusado(f"O arquivo passa de {limite_mb} MB. Envie um arquivo menor.")
    tipo = detectar_tipo(conteudo, ext)
    if tipo is None or tipo not in aceitos:
        raise ArquivoRecusado(f"Tipo de arquivo não aceito. Envie {descricao_aceitos}.")
    if ext not in tipo.extensoes:
        raise ArquivoRecusado(
            f"O conteúdo é de {tipo.descricao}, mas o nome termina em {ext or 'nada'}."
            f" Use a extensão {sorted(tipo.extensoes)[0]}."
        )
    return limpo, tipo
