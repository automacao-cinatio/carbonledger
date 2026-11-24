"""Certificado A1 da empresa e assinatura PAdES do relatorio (sem banco)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from asn1crypto import x509 as asn1_x509
from cryptography.hazmat.primitives import serialization
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.pdf_utils.writer import PdfFileWriter
from pyhanko.sign.validation import async_validate_pdf_signature
from pyhanko_certvalidator import ValidationContext

from app.core.erros import ErroValidacao
from app.modules.relatorios import certificados, relatorio
from tests.certificados_teste import SENHA, gerar_certificado, gerar_pfx

CNPJ = "11222333000181"


def _ler(pfx: bytes, senha: str = SENHA, **opcoes: object) -> certificados.CertificadoLido:
    argumentos: dict[str, object] = {"cnpj_empresa": CNPJ, "exigir_cnpj": False, **opcoes}
    return certificados.ler_pkcs12(pfx, senha, **argumentos)  # type: ignore[arg-type]


def test_le_e_confere_o_e_cnpj() -> None:
    pfx, certificado = gerar_pfx(cnpj=CNPJ)
    lido = _ler(pfx, exigir_cnpj=True)
    assert lido.cnpj == CNPJ and lido.titular == "EMPRESA TESTE SA"
    assert lido.valido_ate == certificado.not_valid_after_utc
    assert len(lido.impressao_sha256) == 32
    # O que fica guardado abre sem senha: a senha nao e guardada.
    from cryptography.hazmat.primitives.serialization import pkcs12

    chave, de_novo, _ = pkcs12.load_key_and_certificates(lido.pkcs12_sem_senha, None)
    assert chave is not None and de_novo == certificado


def test_chave_de_curva_eliptica_tambem_serve() -> None:
    pfx, _ = gerar_pfx(cnpj=CNPJ, curva=True)
    assert _ler(pfx).cnpj == CNPJ


@pytest.mark.parametrize(
    ("pfx_opcoes", "senha", "leitura", "campo", "trecho"),
    [
        ({}, "errada", {}, "senha", "Confira a senha"),
        ({"cnpj": "99888777000166"}, SENHA, {}, "arquivo", "outro CNPJ"),
        ({}, SENHA, {"exigir_cnpj": True}, "arquivo", "e-CNPJ ICP-Brasil"),
        ({"dias": 1, "inicio": datetime(2020, 1, 1, tzinfo=UTC)}, SENHA, {}, "arquivo", "venceu"),
        (
            {"inicio": datetime.now(UTC) + timedelta(days=10)},
            SENHA,
            {},
            "arquivo",
            "ainda não está valendo",
        ),
        ({"pode_assinar": False}, SENHA, {}, "arquivo", "não serve para assinar"),
    ],
)
def test_recusas_dizem_como_corrigir(
    pfx_opcoes: dict[str, object],
    senha: str,
    leitura: dict[str, object],
    campo: str,
    trecho: str,
) -> None:
    pfx, _ = gerar_pfx(**pfx_opcoes)
    with pytest.raises(ErroValidacao) as erro:
        _ler(pfx, senha, **leitura)
    assert erro.value.campo == campo and trecho in erro.value.mensagem


def test_arquivo_vazio_grande_ou_que_nao_e_pfx() -> None:
    with pytest.raises(ErroValidacao, match="Escolha o arquivo"):
        _ler(b"")
    with pytest.raises(ErroValidacao, match="64 KB"):
        _ler(b"0" * (certificados.LIMITE_BYTES + 1))
    with pytest.raises(ErroValidacao, match="Confira a senha"):
        _ler(b"%PDF-1.7 isto nao e um certificado")


def test_certificado_sem_chave_privada_e_recusado() -> None:
    from cryptography.hazmat.primitives.serialization import pkcs12

    certificado, _ = gerar_certificado(cnpj=CNPJ)
    so_certificado = pkcs12.serialize_key_and_certificates(
        b"x", None, certificado, None, serialization.BestAvailableEncryption(SENHA.encode())
    )
    with pytest.raises(ErroValidacao, match="chave privada"):
        _ler(so_certificado)


def _pdf_simples() -> bytes:
    escritor = PdfFileWriter()
    escritor.insert_page(_pagina_vazia(escritor))  # type: ignore[arg-type]
    saida = BytesIO()
    escritor.write(saida)
    return saida.getvalue()


def _pagina_vazia(escritor: PdfFileWriter) -> object:
    from pyhanko.pdf_utils import generic

    return generic.DictionaryObject(
        {
            generic.NameObject("/Type"): generic.NameObject("/Page"),
            generic.NameObject("/MediaBox"): generic.ArrayObject(
                [generic.NumberObject(n) for n in (0, 0, 595, 842)]
            ),
            generic.NameObject("/Resources"): generic.DictionaryObject(),
        }
    )


async def _validar(assinado: bytes, raiz: bytes) -> object:
    leitor = PdfFileReader(BytesIO(assinado))
    assert len(leitor.embedded_signatures) == 1
    contexto = ValidationContext(trust_roots=[asn1_x509.Certificate.load(raiz)])
    return await async_validate_pdf_signature(
        leitor.embedded_signatures[0], signer_validation_context=contexto
    )


async def test_assinatura_pades_valida_e_detecta_alteracao() -> None:
    pfx, certificado = gerar_pfx(cnpj=CNPJ)
    lido = _ler(pfx)
    assinado = await relatorio.assinar_pdf(
        _pdf_simples(), lido.pkcs12_sem_senha, titular=lido.titular, motivo="Teste"
    )
    raiz = certificado.public_bytes(serialization.Encoding.DER)
    estado = await _validar(assinado, raiz)
    assert estado.intact and estado.valid and estado.trusted  # type: ignore[attr-defined]
    assert estado.coverage.name == "ENTIRE_FILE"  # type: ignore[attr-defined]
    leitor = PdfFileReader(BytesIO(assinado))
    assinatura = leitor.embedded_signatures[0]
    assert assinatura.field_name == relatorio.CAMPO_ASSINATURA
    assert "/ETSI.CAdES.detached" in str(assinatura.sig_object["/SubFilter"])

    # Um byte trocado dentro da parte assinada: a assinatura deixa de conferir.
    posicao = assinado.index(b"/MediaBox")
    adulterado = assinado[:posicao] + b"/MediaBoX" + assinado[posicao + 9 :]
    estado_adulterado = await _validar(adulterado, raiz)
    assert not estado_adulterado.intact  # type: ignore[attr-defined]


async def test_relatorio_em_pdf_com_o_tema_e_sem_rede() -> None:
    _pfx, _certificado = gerar_pfx(cnpj=CNPJ)
    certificado = certificados.Certificado(
        certificado_id=uuid4(),
        empresa_id=uuid4(),
        titular="EMPRESA TESTE SA",
        cnpj=CNPJ,
        emissor="AC Teste",
        numero_serie="1",
        valido_de=datetime.now(UTC),
        valido_ate=datetime.now(UTC) + timedelta(days=30),
        impressao_sha256=bytes(32),
        enviado_em=datetime.now(UTC),
        desativado_em=None,
    )
    dados = {
        "empresa": "Empresa <Teste>",
        "cnpj": "11.222.333/0001-81",
        "ano": 2025,
        "abordagem": "Controle operacional",
        "empresa_id": certificado.empresa_id,
        "versao": 1,
        "fechado_em": "31/01/2026 10:00",
        "hash_fechamento": bytes(range(32)),
        "calculo_id": uuid4(),
        "metodo": "fator_direto 1.2.0",
        "gwp": "AR5, 100 anos",
        "conhecido_em": "31/01/2026 09:00",
        "hash_entrada": "ab" * 32,
        "hash_resultado": "cd" * 32,
        "escopo1": Decimal("1234.5678"),
        "escopo2": Decimal("10"),
        "escopo3": Decimal("0"),
        "biogenico": Decimal("2.5"),
        "por_categoria": [{"nome": "Combustão estacionária", "valor": Decimal("1234.5678")}],
        "por_gas": [{"nome": "CO2", "valor": Decimal("1234.5678")}],
        "unidades": [
            {"nome": "Fábrica", "municipio": "Campinas", "uf": "SP",
             "participacao_societaria": Decimal("100")}
        ],
    }  # fmt: skip
    html = relatorio.montar_html(dados, certificado)
    assert "1.234,568" in html and "Empresa &lt;Teste&gt;" in html
    assert "--cl-fonte" in html and "http" not in html.split("<body", 1)[1]
    assert "não certifica" in html and "CNPJ 11.222.333/0001-81" in html
    pdf = await relatorio.gerar_pdf(html)
    assert pdf.startswith(b"%PDF-") and len(pdf) > 1000
