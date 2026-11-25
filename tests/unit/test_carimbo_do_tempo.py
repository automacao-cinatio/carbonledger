"""Carimbo do tempo no relatorio assinado (PAdES B-T): pela carimbadora de teste e por HTTP
contra uma TSA local (RFC 3161), validacao do token, hora registrada e TSA fora do ar."""

import asyncio
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from asn1crypto import keys as asn1_keys
from asn1crypto import tsp
from asn1crypto import x509 as asn1_x509
from cryptography.hazmat.primitives import serialization
from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.pdf_utils.writer import PdfFileWriter
from pyhanko.sign.timestamps import DummyTimeStamper
from pyhanko.sign.timestamps.common_utils import TimestampRequestError
from pyhanko.sign.validation import async_validate_pdf_signature
from pyhanko_certvalidator import ValidationContext

from app.modules.relatorios import certificados, relatorio
from tests.certificados_teste import SENHA, gerar_pfx, gerar_tsa

CNPJ = "11222333000181"
HORA_FIXA = datetime(2026, 11, 24, 15, 30, tzinfo=UTC)


def _pdf() -> bytes:
    escritor = PdfFileWriter()
    escritor.insert_page(  # type: ignore[arg-type]
        generic.DictionaryObject(
            {
                generic.NameObject("/Type"): generic.NameObject("/Page"),
                generic.NameObject("/MediaBox"): generic.ArrayObject(
                    [generic.NumberObject(n) for n in (0, 0, 595, 842)]
                ),
                generic.NameObject("/Resources"): generic.DictionaryObject(),
            }
        )
    )
    saida = BytesIO()
    escritor.write(saida)
    return saida.getvalue()


def _asn1(certificado: Any) -> asn1_x509.Certificate:
    return asn1_x509.Certificate.load(certificado.public_bytes(serialization.Encoding.DER))


def _carimbadora(hora: datetime | None = HORA_FIXA) -> tuple[DummyTimeStamper, Any]:
    tsa, chave = gerar_tsa()
    chave_asn1 = asn1_keys.PrivateKeyInfo.load(
        chave.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return DummyTimeStamper(tsa_cert=_asn1(tsa), tsa_key=chave_asn1, fixed_dt=hora), tsa


def _assinante() -> tuple[bytes, Any]:
    pfx, certificado = gerar_pfx(cnpj=CNPJ)
    lido = certificados.ler_pkcs12(pfx, SENHA, cnpj_empresa=CNPJ, exigir_cnpj=False)
    return lido.pkcs12_sem_senha, certificado


async def _validar(assinado: bytes, raiz: Any, raiz_tsa: Any) -> Any:
    assinatura = PdfFileReader(BytesIO(assinado)).embedded_signatures[0]
    return await async_validate_pdf_signature(
        assinatura,
        signer_validation_context=ValidationContext(trust_roots=[_asn1(raiz)]),
        ts_validation_context=ValidationContext(trust_roots=[_asn1(raiz_tsa)]),
    )


async def test_assinatura_com_carimbo_b_t() -> None:
    pkcs12, certificado = _assinante()
    carimbadora, tsa = _carimbadora()
    assinado = await relatorio.assinar_pdf(
        _pdf(), pkcs12, titular="EMPRESA", motivo="Teste", carimbadora=carimbadora
    )
    estado = await _validar(assinado, certificado, tsa)
    assert estado.intact and estado.valid
    assert estado.timestamp_validity is not None and estado.timestamp_validity.valid
    assert estado.timestamp_validity.trusted
    assert relatorio.carimbo_do_pdf(assinado) == HORA_FIXA


async def test_sem_carimbadora_sai_b_b_sem_hora() -> None:
    pkcs12, _ = _assinante()
    assinado = await relatorio.assinar_pdf(_pdf(), pkcs12, titular="EMPRESA", motivo="Teste")
    assert relatorio.carimbo_do_pdf(assinado) is None


class TSAForaDoAr(DummyTimeStamper):
    async def async_request_tsa_response(self, req: Any) -> Any:
        raise TimestampRequestError("TSA respondeu 503")


async def test_tsa_fora_do_ar_nao_gera_sem_carimbo() -> None:
    pkcs12, _ = _assinante()
    base, _tsa = _carimbadora()
    fora = TSAForaDoAr(tsa_cert=base.tsa_cert, tsa_key=base.tsa_key)
    with pytest.raises(relatorio.CarimboIndisponivel):
        await relatorio.assinar_pdf(
            _pdf(), pkcs12, titular="EMPRESA", motivo="Teste", carimbadora=fora
        )


@pytest.fixture
def tsa_http() -> Iterator[tuple[str, Any]]:
    """TSA local por HTTP (RFC 3161): recebe o TimeStampReq e responde com a de teste."""
    carimbadora, tsa = _carimbadora(hora=None)

    class Tratador(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            pedido = tsp.TimeStampReq.load(self.rfile.read(int(self.headers["Content-Length"])))
            resposta = asyncio.run(carimbadora.async_request_tsa_response(pedido))
            corpo = resposta.dump()
            self.send_response(200)
            self.send_header("Content-Type", "application/timestamp-reply")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def log_message(self, *_: Any) -> None:
            pass

    servidor = HTTPServer(("127.0.0.1", 0), Tratador)
    linha = threading.Thread(target=servidor.serve_forever, daemon=True)
    linha.start()
    try:
        yield f"http://127.0.0.1:{servidor.server_port}/tsa", tsa
    finally:
        servidor.shutdown()


async def test_carimbo_por_http_pela_configuracao(tsa_http: tuple[str, Any]) -> None:
    url, tsa = tsa_http
    configuracao = SimpleNamespace(
        tsa_url=url, tsa_usuario=None, tsa_senha=None, tsa_tempo_limite_s=10.0
    )
    carimbadora = relatorio.carimbadora_da_configuracao(configuracao)
    assert carimbadora is not None
    pkcs12, certificado = _assinante()
    antes = datetime.now(UTC) - timedelta(seconds=5)
    assinado = await relatorio.assinar_pdf(
        _pdf(), pkcs12, titular="EMPRESA", motivo="Teste", carimbadora=carimbadora
    )
    estado = await _validar(assinado, certificado, tsa)
    assert estado.timestamp_validity is not None and estado.timestamp_validity.valid
    hora = relatorio.carimbo_do_pdf(assinado)
    assert hora is not None and antes <= hora <= datetime.now(UTC) + timedelta(seconds=5)


async def test_tsa_http_inacessivel_vira_carimbo_indisponivel() -> None:
    configuracao = SimpleNamespace(
        tsa_url="http://127.0.0.1:9/tsa", tsa_usuario=None, tsa_senha=None, tsa_tempo_limite_s=2
    )
    pkcs12, _ = _assinante()
    with pytest.raises(relatorio.CarimboIndisponivel):
        await relatorio.assinar_pdf(
            _pdf(),
            pkcs12,
            titular="EMPRESA",
            motivo="Teste",
            carimbadora=relatorio.carimbadora_da_configuracao(configuracao),
        )


def test_sem_tsa_url_nao_ha_carimbadora() -> None:
    assert relatorio.carimbadora_da_configuracao(SimpleNamespace(tsa_url=None)) is None
