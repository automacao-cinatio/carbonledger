"""Cifra envelope com chaves mestras do KeyProvider (local em dev; KMS da AWS em producao).

Cada valor ganha uma chave de dados nova (AES-256-GCM). A chave de dados vai cifrada pela
chave mestra, e o contexto (com o tenant) entra como dado associado nas duas cifras: um
envelope de um tenant nao abre com o contexto de outro.

Formato do envelope (texto): "cl1." + base64url(JSON {"k": id da chave mestra,
"dk": chave de dados cifrada, "n": nonce, "c": dados cifrados}).

KMS: a chave de dados e cifrada pelo KMS (Encrypt/Decrypt) com o contexto canonico como
EncryptionContext, que o KMS amarra ao ciphertext e registra no CloudTrail. O id da mestra no
envelope e o ARN da chave que cifrou, para a rotacao continuar abrindo envelopes antigos. Em
dev, o mesmo codigo fala com o local-kms do docker compose (KMS_ENDPOINT).
"""

import base64
import binascii
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIXO = "cl1."
TAMANHO_CHAVE = 32
TAMANHO_NONCE = 12


class ErroChave(Exception):
    """Envelope invalido, contexto diferente, chave desconhecida ou configuracao errada."""


class ProvedorChaves(Protocol):
    def cifrar_chave(self, chave: bytes, contexto: bytes) -> tuple[str, bytes]:
        """Cifra uma chave de dados com a chave mestra atual: (id da mestra, cifrada)."""
        ...

    def decifrar_chave(self, id_mestra: str, cifrada: bytes, contexto: bytes) -> bytes: ...


def _b64(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).rstrip(b"=").decode("ascii")


def _de_b64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def _contexto_canonico(contexto: Mapping[str, str]) -> bytes:
    if "tenant_id" not in contexto or "finalidade" not in contexto:
        raise ErroChave("o contexto precisa de tenant_id e finalidade")
    return json.dumps(dict(contexto), sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class ProvedorChavesLocal:
    """Chaves mestras vindas de variavel de ambiente. So para desenvolvimento e testes.

    CHAVES_LOCAIS="id-atual:base64,id-antigo:base64": a primeira cifra; todas decifram.
    """

    chaves: Mapping[str, bytes]
    atual: str

    @classmethod
    def da_configuracao(cls, texto: str) -> "ProvedorChavesLocal":
        chaves: dict[str, bytes] = {}
        ordem: list[str] = []
        for item in (parte.strip() for parte in texto.split(",") if parte.strip()):
            id_chave, _, codificada = item.partition(":")
            try:
                chave = base64.b64decode(codificada, validate=True)
            except (binascii.Error, ValueError) as erro:
                raise ErroChave(f"chave local {id_chave!r} nao e base64") from erro
            if not id_chave or len(chave) != TAMANHO_CHAVE:
                raise ErroChave(f"chave local {id_chave!r} precisa de id e 32 bytes")
            if id_chave in chaves:
                raise ErroChave(f"chave local {id_chave!r} repetida")
            chaves[id_chave] = chave
            ordem.append(id_chave)
        if not ordem:
            raise ErroChave("CHAVES_LOCAIS vazio")
        return cls(chaves=chaves, atual=ordem[0])

    def cifrar_chave(self, chave: bytes, contexto: bytes) -> tuple[str, bytes]:
        nonce = os.urandom(TAMANHO_NONCE)
        return self.atual, nonce + AESGCM(self.chaves[self.atual]).encrypt(nonce, chave, contexto)

    def decifrar_chave(self, id_mestra: str, cifrada: bytes, contexto: bytes) -> bytes:
        mestra = self.chaves.get(id_mestra)
        if mestra is None:
            raise ErroChave(f"chave mestra desconhecida: {id_mestra}")
        try:
            return AESGCM(mestra).decrypt(
                cifrada[:TAMANHO_NONCE], cifrada[TAMANHO_NONCE:], contexto
            )
        except InvalidTag as erro:
            raise ErroChave("chave de dados nao confere com o contexto") from erro


def cifrar(provedor: ProvedorChaves, dados: bytes, contexto: Mapping[str, str]) -> str:
    aad = _contexto_canonico(contexto)
    chave_dados = AESGCM.generate_key(bit_length=256)
    id_mestra, chave_cifrada = provedor.cifrar_chave(chave_dados, aad)
    nonce = os.urandom(TAMANHO_NONCE)
    cifrado = AESGCM(chave_dados).encrypt(nonce, dados, aad)
    corpo = {"k": id_mestra, "dk": _b64(chave_cifrada), "n": _b64(nonce), "c": _b64(cifrado)}
    return PREFIXO + _b64(json.dumps(corpo, separators=(",", ":")).encode("utf-8"))


def decifrar(provedor: ProvedorChaves, envelope: str, contexto: Mapping[str, str]) -> bytes:
    aad = _contexto_canonico(contexto)
    if not envelope.startswith(PREFIXO):
        raise ErroChave("envelope em formato desconhecido")
    try:
        corpo = json.loads(_de_b64(envelope[len(PREFIXO) :]))
        id_mestra, chave_cifrada = str(corpo["k"]), _de_b64(corpo["dk"])
        nonce, cifrado = _de_b64(corpo["n"]), _de_b64(corpo["c"])
    except (ValueError, KeyError, TypeError, binascii.Error) as erro:
        raise ErroChave("envelope corrompido") from erro
    chave_dados = provedor.decifrar_chave(id_mestra, chave_cifrada, aad)
    try:
        return AESGCM(chave_dados).decrypt(nonce, cifrado, aad)
    except InvalidTag as erro:
        raise ErroChave("dados nao conferem com o contexto") from erro


class ProvedorChavesKMS:
    """Chave mestra no AWS KMS (ou no local-kms em dev). A chave nunca sai do KMS."""

    def __init__(self, chave_id: str, cliente: Any) -> None:
        if not chave_id:
            raise ErroChave("defina KMS_CHAVE_ID (ARN, id ou alias da chave)")
        self.chave_id = chave_id
        self._cliente = cliente

    @classmethod
    def da_configuracao(
        cls, chave_id: str, regiao: str, endpoint: str | None = None
    ) -> "ProvedorChavesKMS":
        import boto3
        from botocore.config import Config as ConfigBotocore

        cliente = boto3.client(
            "kms",
            region_name=regiao,
            endpoint_url=endpoint or None,
            config=ConfigBotocore(connect_timeout=5, read_timeout=10, retries={"max_attempts": 3}),
        )
        return cls(chave_id, cliente)

    @staticmethod
    def _contexto_kms(contexto: bytes) -> dict[str, str]:
        return {"carbonledger": contexto.decode("utf-8")}

    def cifrar_chave(self, chave: bytes, contexto: bytes) -> tuple[str, bytes]:
        try:
            resposta = self._cliente.encrypt(
                KeyId=self.chave_id,
                Plaintext=chave,
                EncryptionContext=self._contexto_kms(contexto),
            )
        except Exception as erro:  # erro de rede, permissao ou chave: nunca cifra sem KMS
            raise ErroChave(f"KMS recusou cifrar ({type(erro).__name__})") from erro
        return str(resposta["KeyId"]), bytes(resposta["CiphertextBlob"])

    def decifrar_chave(self, id_mestra: str, cifrada: bytes, contexto: bytes) -> bytes:
        try:
            resposta = self._cliente.decrypt(
                KeyId=id_mestra,
                CiphertextBlob=cifrada,
                EncryptionContext=self._contexto_kms(contexto),
            )
        except Exception as erro:  # contexto diferente cai aqui (InvalidCiphertextException)
            raise ErroChave(f"KMS recusou decifrar ({type(erro).__name__})") from erro
        return bytes(resposta["Plaintext"])


def obter_provedor(provedor: str, chaves_locais: str | None) -> ProvedorChaves:
    if provedor == "local":
        if not chaves_locais:
            raise ErroChave("defina CHAVES_LOCAIS para o provedor local")
        return ProvedorChavesLocal.da_configuracao(chaves_locais)
    raise ErroChave(f"provedor de chaves {provedor!r} desconhecido (use local ou kms)")


def provedor_da_configuracao(configuracao: Any) -> ProvedorChaves | None:
    """Provedor do app, ou None se as chaves nao estao configuradas (sem cifra, sem assinatura).

    Chave configurada errada nao vira None: o app nao sobe, para o erro aparecer cedo.
    """
    if configuracao.provedor_chaves == "kms":
        return ProvedorChavesKMS.da_configuracao(
            configuracao.kms_chave_id or "",
            configuracao.kms_regiao,
            configuracao.kms_endpoint,
        )
    locais = configuracao.chaves_locais
    if configuracao.provedor_chaves != "local" or locais is None:
        return None
    return obter_provedor("local", locais.get_secret_value())
