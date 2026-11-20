import base64
import os

import pytest

from app.core.keys import ErroChave, ProvedorChavesLocal, cifrar, decifrar, obter_provedor

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


def test_kms_ainda_nao_existe() -> None:
    with pytest.raises(ErroChave, match="P25"):
        obter_provedor("kms", None)
    with pytest.raises(ErroChave):
        obter_provedor("local", None)
