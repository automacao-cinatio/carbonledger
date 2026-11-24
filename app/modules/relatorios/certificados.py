"""Certificado A1 de cada empresa, para assinar o relatorio do inventario (P19).

Cada cliente assina com o proprio certificado (decisao do usuario). O .pfx chega com a senha,
e aberto aqui, conferido e guardado so cifrado (cifra envelope, contexto com tenant, empresa,
certificado e finalidade), ja sem senha: a senha nao e guardada. Desativar apaga a chave
cifrada; os dados publicos ficam, porque os relatorios antigos apontam para eles.

Conferencias: abre com a senha; tem chave e certificado que combinam; chave RSA ou EC; dentro
da validade; uso de chave que permite assinar; CNPJ do e-CNPJ ICP-Brasil (OID 2.16.76.1.3.3)
igual ao da empresa. Certificado sem CNPJ so e aceito fora de producao.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from asn1crypto import core
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, keys
from app.core.antivirus import Antivirus, AntivirusIndisponivel, ArquivoInfectado
from app.core.arquivos import ArquivoRecusado, extensao, nome_seguro
from app.core.erros import AcessoNegado, ErroValidacao
from app.core.security.acesso import Acesso

OID_CNPJ_ICP_BRASIL = x509.ObjectIdentifier("2.16.76.1.3.3")
EXTENSOES_ACEITAS = frozenset({".pfx", ".p12"})
LIMITE_BYTES = 64 * 1024
TAMANHO_MAXIMO_SENHA = 200
FINALIDADE = "certificado_assinatura"


@dataclass(frozen=True)
class CertificadoLido:
    """O que sai de um .pfx conferido, antes de gravar."""

    titular: str
    cnpj: str | None
    emissor: str
    numero_serie: str
    valido_de: datetime
    valido_ate: datetime
    impressao_sha256: bytes
    pkcs12_sem_senha: bytes


@dataclass(frozen=True)
class Certificado:
    certificado_id: UUID
    empresa_id: UUID
    titular: str
    cnpj: str | None
    emissor: str
    numero_serie: str
    valido_de: datetime
    valido_ate: datetime
    impressao_sha256: bytes
    enviado_em: datetime
    desativado_em: datetime | None

    @property
    def ativo(self) -> bool:
        return self.desativado_em is None

    def vigente(self, agora: datetime | None = None) -> bool:
        momento = agora or datetime.now(UTC)
        return self.ativo and self.valido_de <= momento < self.valido_ate


def _recusa(mensagem: str) -> ErroValidacao:
    return ErroValidacao("arquivo", mensagem)


def _nome(nome: x509.Name) -> str:
    comuns = nome.get_attributes_for_oid(NameOID.COMMON_NAME)
    if comuns:
        valor = comuns[0].value
        return valor if isinstance(valor, str) else valor.decode("utf-8", "replace")
    return nome.rfc4514_string()


def cnpj_do_certificado(certificado: x509.Certificate) -> str | None:
    """CNPJ do e-CNPJ ICP-Brasil (otherName 2.16.76.1.3.3 no nome alternativo), se houver."""
    try:
        alternativos = certificado.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except x509.ExtensionNotFound:
        return None
    for outro in alternativos.get_values_for_type(x509.OtherName):
        if outro.type_id != OID_CNPJ_ICP_BRASIL:
            continue
        try:
            valor = core.Asn1Value.load(outro.value)
            bruto = valor.contents if valor.contents is not None else b""
        except ValueError:
            continue
        digitos = "".join(c for c in bruto.decode("latin-1") if c.isdigit())
        if len(digitos) == 14:
            return digitos
    return None


def _pode_assinar(certificado: x509.Certificate) -> bool:
    try:
        uso = certificado.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        return True
    return bool(uso.digital_signature or uso.content_commitment)


def _publica(chave: Any) -> bytes:
    return bytes(
        chave.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )


def ler_pkcs12(
    conteudo: bytes,
    senha: str,
    *,
    cnpj_empresa: str | None,
    exigir_cnpj: bool,
    agora: datetime | None = None,
) -> CertificadoLido:
    """Abre e confere o .pfx. Qualquer recusa vira erro do campo, dizendo como corrigir."""
    if not conteudo:
        raise _recusa("Escolha o arquivo do certificado (.pfx ou .p12).")
    if len(conteudo) > LIMITE_BYTES:
        raise _recusa("O arquivo é grande demais para um certificado A1 (máximo de 64 KB).")
    if len(senha) > TAMANHO_MAXIMO_SENHA:
        raise ErroValidacao("senha", "A senha passou do tamanho máximo.")
    try:
        chave, certificado, cadeia = pkcs12.load_key_and_certificates(
            conteudo, senha.encode("utf-8") or None
        )
    except (ValueError, TypeError) as erro:
        raise ErroValidacao(
            "senha",
            "Não foi possível abrir o certificado. Confira a senha e se o arquivo é o .pfx"
            " ou .p12 exportado com a chave privada.",
        ) from erro
    if chave is None or certificado is None:
        raise _recusa("O arquivo não traz a chave privada. Exporte o certificado com a chave.")
    if not isinstance(chave, rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey):
        raise _recusa("Só certificados com chave RSA ou de curva elíptica são aceitos.")
    if _publica(chave.public_key()) != _publica(certificado.public_key()):
        raise _recusa("A chave privada não é a deste certificado. Exporte o .pfx de novo.")
    momento = agora or datetime.now(UTC)
    if momento < certificado.not_valid_before_utc:
        raise _recusa("O certificado ainda não está valendo.")
    if momento >= certificado.not_valid_after_utc:
        vencido = certificado.not_valid_after_utc.strftime("%d/%m/%Y")
        raise _recusa(f"O certificado venceu em {vencido}. Envie um certificado válido.")
    if not _pode_assinar(certificado):
        raise _recusa("Este certificado não serve para assinar documentos.")
    cnpj = cnpj_do_certificado(certificado)
    if cnpj is None and exigir_cnpj:
        raise _recusa("Use o e-CNPJ ICP-Brasil da empresa: este certificado não tem CNPJ.")
    if cnpj is not None and cnpj != cnpj_empresa:
        raise _recusa(
            "O certificado é de outro CNPJ. Use o e-CNPJ da própria empresa, com o CNPJ"
            " cadastrado na organização."
        )
    sem_senha = pkcs12.serialize_key_and_certificates(
        None, chave, certificado, list(cadeia) or None, serialization.NoEncryption()
    )
    return CertificadoLido(
        titular=_nome(certificado.subject),
        cnpj=cnpj,
        emissor=_nome(certificado.issuer),
        numero_serie=format(certificado.serial_number, "x"),
        valido_de=certificado.not_valid_before_utc,
        valido_ate=certificado.not_valid_after_utc,
        impressao_sha256=hashlib.sha256(
            certificado.public_bytes(serialization.Encoding.DER)
        ).digest(),
        pkcs12_sem_senha=sem_senha,
    )


def contexto_cifra(tenant_id: UUID, empresa_id: UUID, certificado_id: UUID) -> Mapping[str, str]:
    return {
        "tenant_id": str(tenant_id),
        "empresa_id": str(empresa_id),
        "certificado_id": str(certificado_id),
        "finalidade": FINALIDADE,
    }


_COLUNAS = (
    "certificado_id, empresa_id, titular, cnpj, emissor, numero_serie, valido_de, valido_ate,"
    " impressao_sha256, enviado_em, desativado_em"
)


def _do_registro(linha: Any) -> Certificado:
    dados = dict(linha._asdict())
    dados["impressao_sha256"] = bytes(dados["impressao_sha256"])
    return Certificado(**dados)


async def ativo_da_empresa(sessao: AsyncSession, empresa_id: UUID) -> Certificado | None:
    linha = (
        await sessao.execute(
            text(
                f"SELECT {_COLUNAS} FROM certificado_assinatura"  # noqa: S608  # nosec B608
                " WHERE empresa_id = :empresa AND desativado_em IS NULL"
            ),
            {"empresa": empresa_id},
        )
    ).first()
    return None if linha is None else _do_registro(linha)


async def obter(sessao: AsyncSession, certificado_id: UUID) -> Certificado:
    linha = (
        await sessao.execute(
            text(
                f"SELECT {_COLUNAS} FROM certificado_assinatura"  # noqa: S608  # nosec B608
                " WHERE certificado_id = :id"
            ),
            {"id": certificado_id},
        )
    ).first()
    if linha is None:
        raise AcessoNegado
    return _do_registro(linha)


async def cadastrar(
    sessao: AsyncSession,
    acesso: Acesso,
    empresa_id: UUID,
    cnpj_empresa: str | None,
    nome_arquivo: str | None,
    conteudo: bytes,
    senha: str,
    *,
    provedor: keys.ProvedorChaves,
    antivirus: Antivirus,
    exigir_cnpj: bool,
) -> UUID:
    """Confere, cifra e grava; o certificado ativo anterior da empresa e desativado."""
    try:
        nome = nome_seguro(nome_arquivo)
    except ArquivoRecusado as erro:
        raise _recusa(str(erro)) from erro
    if extensao(nome) not in EXTENSOES_ACEITAS:
        raise _recusa("Envie o certificado A1 no formato .pfx ou .p12.")
    lido = ler_pkcs12(conteudo, senha, cnpj_empresa=cnpj_empresa, exigir_cnpj=exigir_cnpj)
    try:
        await antivirus.verificar(conteudo)
    except ArquivoInfectado as erro:
        raise _recusa(
            "O antivírus encontrou uma ameaça neste arquivo. Ele não foi guardado."
        ) from erro
    except AntivirusIndisponivel as erro:
        raise _recusa(
            "Não foi possível verificar o arquivo agora. Tente de novo em alguns minutos."
        ) from erro
    anterior = await ativo_da_empresa(sessao, empresa_id)
    if anterior is not None:
        await desativar(sessao, acesso, anterior.certificado_id, motivo="substituido")
    certificado_id = uuid4()
    cifrado = keys.cifrar(
        provedor,
        lido.pkcs12_sem_senha,
        contexto_cifra(acesso.tenant_id, empresa_id, certificado_id),
    )
    await sessao.execute(
        text(
            "INSERT INTO certificado_assinatura (tenant_id, certificado_id, empresa_id, titular,"
            " cnpj, emissor, numero_serie, valido_de, valido_ate, impressao_sha256,"
            " chave_cifrada, enviado_por) VALUES (:tenant, :id, :empresa, :titular, :cnpj,"
            " :emissor, :serie, :de, :ate, :impressao, :cifrada, :usuario)"
        ),
        {
            "tenant": acesso.tenant_id,
            "id": certificado_id,
            "empresa": empresa_id,
            "titular": lido.titular,
            "cnpj": lido.cnpj,
            "emissor": lido.emissor,
            "serie": lido.numero_serie,
            "de": lido.valido_de,
            "ate": lido.valido_ate,
            "impressao": lido.impressao_sha256,
            "cifrada": cifrado,
            "usuario": acesso.usuario_id,
        },
    )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="certificado.cadastrado",
        entidade="certificado_assinatura",
        entidade_id=certificado_id,
        dados={
            "empresa_id": str(empresa_id),
            "impressao_sha256": lido.impressao_sha256.hex(),
            "valido_ate": lido.valido_ate.isoformat(),
            "icp_brasil_cnpj": lido.cnpj is not None,
        },
    )
    return certificado_id


async def desativar(
    sessao: AsyncSession, acesso: Acesso, certificado_id: UUID, *, motivo: str = "manual"
) -> None:
    certificado = await obter(sessao, certificado_id)
    if not certificado.ativo:
        raise ErroValidacao("certificado", "Este certificado já está desativado.")
    await sessao.execute(
        text(
            "UPDATE certificado_assinatura SET chave_cifrada = NULL, desativado_em = now(),"
            " desativado_por = :usuario WHERE certificado_id = :id"
        ),
        {"usuario": acesso.usuario_id, "id": certificado_id},
    )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="certificado.desativado",
        entidade="certificado_assinatura",
        entidade_id=certificado_id,
        dados={"empresa_id": str(certificado.empresa_id), "motivo": motivo},
    )


async def pkcs12_para_assinar(
    conexao: Any, provedor: keys.ProvedorChaves, tenant_id: UUID, certificado_id: UUID
) -> bytes:
    """Decifra o .pfx (sem senha) do certificado ativo, so na hora de assinar (asyncpg)."""
    linha = await conexao.fetchrow(
        "SELECT empresa_id, chave_cifrada FROM certificado_assinatura"
        " WHERE certificado_id = $1 AND desativado_em IS NULL",
        certificado_id,
    )
    if linha is None or linha["chave_cifrada"] is None:
        raise AcessoNegado
    return keys.decifrar(
        provedor,
        linha["chave_cifrada"],
        contexto_cifra(tenant_id, linha["empresa_id"], certificado_id),
    )
