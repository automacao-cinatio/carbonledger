import base64
import json
import os

import pytest

from app.core.keys import (
    ErroChave,
    ProvedorChavesKMS,
    ProvedorChavesLocal,
    cifrar,
    decifrar,
    obter_provedor,
)

CONTEXTO = {"tenant_id": "aaaaaaaa-0000-4000-8000-000000000000", "finalidade": "certificado"}


def _chave() -> str:
    return base64.b64encode(os.urandom(32)).decode()


@pytest.fixture
def provedor() -> ProvedorChavesLocal:
    return ProvedorChavesLocal.da_configuracao(f"k2:{_chave()},k1:{_chave()}")


def test_ida_e_volta(provedor: ProvedorChavesLocal) -> None:
    envelope = cifrar(provedor, b"segredo do cliente", CONTEXTO)
    assert envelope.startswith("cl1.")
    assert b"segredo" not in envelope.encode()
    assert decifrar(provedor, envelope, CONTEXTO) == b"segredo do cliente"


def test_cada_cifra_e_diferente(provedor: ProvedorChavesLocal) -> None:
    assert cifrar(provedor, b"x", CONTEXTO) != cifrar(provedor, b"x", CONTEXTO)


def test_outro_tenant_nao_abre(provedor: ProvedorChavesLocal) -> None:
    envelope = cifrar(provedor, b"x", CONTEXTO)
    with pytest.raises(ErroChave):
        decifrar(
            provedor, envelope, {**CONTEXTO, "tenant_id": "bbbbbbbb-0000-4000-8000-000000000000"}
        )
    with pytest.raises(ErroChave):
        decifrar(provedor, envelope, {**CONTEXTO, "finalidade": "outra"})


def test_envelope_adulterado_nao_abre(provedor: ProvedorChavesLocal) -> None:
    envelope = cifrar(provedor, b"x", CONTEXTO)
    corpo = bytearray(base64.urlsafe_b64decode(envelope[4:] + "=" * (-len(envelope[4:]) % 4)))
    corpo[-5] ^= 1
    adulterado = "cl1." + base64.urlsafe_b64encode(bytes(corpo)).rstrip(b"=").decode()
    with pytest.raises(ErroChave):
        decifrar(provedor, adulterado, CONTEXTO)
    with pytest.raises(ErroChave):
        decifrar(provedor, "cl1.lixo", CONTEXTO)
    with pytest.raises(ErroChave):
        decifrar(provedor, "outro-formato", CONTEXTO)


def test_rotacao_de_chave_mestra() -> None:
    antiga, nova = _chave(), _chave()
    antes = ProvedorChavesLocal.da_configuracao(f"k1:{antiga}")
    envelope_antigo = cifrar(antes, b"x", CONTEXTO)
    depois = ProvedorChavesLocal.da_configuracao(f"k2:{nova},k1:{antiga}")
    assert decifrar(depois, envelope_antigo, CONTEXTO) == b"x"
    assert depois.atual == "k2"
    sem_a_antiga = ProvedorChavesLocal.da_configuracao(f"k2:{nova}")
    with pytest.raises(ErroChave, match="desconhecida"):
        decifrar(sem_a_antiga, envelope_antigo, CONTEXTO)


def test_contexto_precisa_de_tenant_e_finalidade(provedor: ProvedorChavesLocal) -> None:
    with pytest.raises(ErroChave):
        cifrar(provedor, b"x", {"finalidade": "certificado"})
    with pytest.raises(ErroChave):
        cifrar(provedor, b"x", {"tenant_id": "t"})


@pytest.mark.parametrize(
    "texto",
    ["", "k1", "k1:nao-base64!", f"k1:{base64.b64encode(b'curta').decode()}", f":{_chave()}"],
)
def test_configuracao_invalida(texto: str) -> None:
    with pytest.raises(ErroChave):
        ProvedorChavesLocal.da_configuracao(texto)


def test_chave_repetida_e_recusada() -> None:
    chave = _chave()
    with pytest.raises(ErroChave, match="repetida"):
        ProvedorChavesLocal.da_configuracao(f"k1:{chave},k1:{chave}")


def test_provedor_desconhecido_ou_sem_chave() -> None:
    with pytest.raises(ErroChave, match="desconhecido"):
        obter_provedor("cofre", None)
    with pytest.raises(ErroChave):
        obter_provedor("local", None)


class KMSFalso:
    """Imita o Encrypt/Decrypt do KMS: o EncryptionContext precisa ser o mesmo."""

    def __init__(self) -> None:
        self.guardado: dict[bytes, tuple[bytes, dict[str, str]]] = {}
        self.fora_do_ar = False

    def encrypt(self, KeyId: str, Plaintext: bytes, EncryptionContext: dict[str, str]) -> dict:
        if self.fora_do_ar:
            raise ConnectionError("sem rede")
        blob = os.urandom(16)
        self.guardado[blob] = (Plaintext, EncryptionContext)
        return {"KeyId": "arn:aws:kms:sa-east-1:111122223333:key/abc", "CiphertextBlob": blob}

    def decrypt(self, KeyId: str, CiphertextBlob: bytes, EncryptionContext: dict[str, str]) -> dict:
        claro, contexto = self.guardado[CiphertextBlob]
        if contexto != EncryptionContext:
            raise ValueError("InvalidCiphertextException")
        return {"Plaintext": claro, "KeyId": KeyId}


def test_kms_ida_e_volta_e_contexto_amarrado() -> None:
    kms = KMSFalso()
    provedor = ProvedorChavesKMS("alias/carbonledger", kms)
    contexto = {"tenant_id": "t1", "finalidade": "teste"}
    envelope = cifrar(provedor, b"segredo", contexto)
    assert decifrar(provedor, envelope, contexto) == b"segredo"
    # O id da mestra no envelope e o ARN devolvido pelo KMS (rotacao continua abrindo).
    corpo = json.loads(base64.urlsafe_b64decode(envelope[4:] + "=="))
    assert corpo["k"].startswith("arn:aws:kms:")
    with pytest.raises(ErroChave, match="decifrar"):
        decifrar(provedor, envelope, {"tenant_id": "t2", "finalidade": "teste"})
    kms.fora_do_ar = True
    with pytest.raises(ErroChave, match="cifrar"):
        cifrar(provedor, b"x", contexto)
    with pytest.raises(ErroChave, match="KMS_CHAVE_ID"):
        ProvedorChavesKMS("", kms)
