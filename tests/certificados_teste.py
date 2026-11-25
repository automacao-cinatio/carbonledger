"""Certificados A1 de teste (autoassinados), com ou sem o CNPJ ICP-Brasil, para os testes."""

from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

SENHA = "segredo-do-teste"


def _printable(texto: str) -> bytes:
    dados = texto.encode("ascii")
    return bytes([0x13, len(dados)]) + dados


def gerar_certificado(
    *,
    nome: str = "EMPRESA TESTE SA",
    cnpj: str | None = None,
    inicio: datetime | None = None,
    dias: int = 365,
    pode_assinar: bool = True,
    curva: bool = False,
) -> tuple[x509.Certificate, rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey]:
    chave: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey = (
        ec.generate_private_key(ec.SECP256R1())
        if curva
        else rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    sujeito = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, nome)])
    comeco = inicio or datetime.now(UTC) - timedelta(days=1)
    construtor = (
        x509.CertificateBuilder()
        .subject_name(sujeito)
        .issuer_name(sujeito)
        .public_key(chave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(comeco)
        .not_valid_after(comeco + timedelta(days=dias))
        .add_extension(
            x509.KeyUsage(
                digital_signature=pode_assinar,
                content_commitment=pode_assinar,
                key_encipherment=not pode_assinar,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )
    if cnpj is not None:
        construtor = construtor.add_extension(
            x509.SubjectAlternativeName(
                [x509.OtherName(x509.ObjectIdentifier("2.16.76.1.3.3"), _printable(cnpj))]
            ),
            critical=False,
        )
    return construtor.sign(chave, hashes.SHA256()), chave


def gerar_pfx(senha: str = SENHA, **opcoes: object) -> tuple[bytes, x509.Certificate]:
    certificado, chave = gerar_certificado(**opcoes)  # type: ignore[arg-type]
    pfx = pkcs12.serialize_key_and_certificates(
        b"teste",
        chave,
        certificado,
        None,
        serialization.BestAvailableEncryption(senha.encode("utf-8")),
    )
    return pfx, certificado


def gerar_tsa(nome: str = "TSA DE TESTE") -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """Certificado de autoridade de carimbo do tempo (uso estendido timeStamping, critico)."""
    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sujeito = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, nome)])
    agora = datetime.now(UTC) - timedelta(days=1)
    certificado = (
        x509.CertificateBuilder()
        .subject_name(sujeito)
        .issuer_name(sujeito)
        .public_key(chave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(agora)
        .not_valid_after(agora + timedelta(days=365))
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.TIME_STAMPING]), critical=True
        )
        .sign(chave, hashes.SHA256())
    )
    return certificado, chave
