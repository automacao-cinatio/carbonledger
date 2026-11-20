import hashlib
from uuid import uuid4

import pytest

from app.core.security.convite import decodificar_token, novo_token


def test_ida_e_volta() -> None:
    token = novo_token(uuid4(), uuid4())
    texto = token.codificar()
    assert "=" not in texto
    assert decodificar_token(texto) == token
    assert token.segredo_hash == hashlib.sha256(token.segredo).digest()


def test_tokens_diferentes_para_o_mesmo_convite() -> None:
    tenant, convite = uuid4(), uuid4()
    assert novo_token(tenant, convite).segredo != novo_token(tenant, convite).segredo


@pytest.mark.parametrize("texto", ["", "abc", "!!!", "A" * 85, "A" * 87, "A" * 200])
def test_texto_invalido(texto: str) -> None:
    assert decodificar_token(texto) is None
