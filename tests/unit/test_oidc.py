"""Validacao do id_token com chaves geradas no teste e um transporte HTTP simulado."""

import time
from typing import Any

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import KeySet, OctKey, RSAKey
from pydantic import SecretStr

from app.core.config import Configuracao
from app.core.security.oidc import ClienteOidc, ErroOidc, Provedor, provedores_habilitados

EMISSOR = "http://oidc.teste/dev"
TID = "9b1c6a4e-1111-4222-8333-444455556666"
EMISSOR_MS = f"https://login.microsoftonline.com/{TID}/v2.0"
CLIENTE = "carbonledger-dev"


def _provedor(nome: str = "dev") -> Provedor:
    return Provedor(
        nome=nome,
        descoberta_url=f"{EMISSOR}/.well-known/openid-configuration",
        client_id=CLIENTE,
        client_secret=SecretStr("x"),
    )


class Servidor:
    """Faz o papel do provedor: descoberta e JWKS, com chaves trocaveis (rotacao)."""

    def __init__(self) -> None:
        self.chave = RSAKey.generate_key(2048, parameters={"kid": "k1"})
        self.leituras_jwks = 0

    def transporte(self) -> httpx.MockTransport:
        def responder(requisicao: httpx.Request) -> httpx.Response:
            if requisicao.url.path.endswith("openid-configuration"):
                return httpx.Response(
                    200,
                    json={
                        "issuer": EMISSOR,
                        "authorization_endpoint": f"{EMISSOR}/authorize",
                        "token_endpoint": f"{EMISSOR}/token",
                        "jwks_uri": f"{EMISSOR}/jwks",
                    },
                )
            if requisicao.url.path.endswith("/jwks"):
                self.leituras_jwks += 1
                return httpx.Response(200, json=KeySet([self.chave]).as_dict(private=False))
            return httpx.Response(404)

        return httpx.MockTransport(responder)

    def assinar(self, claims: dict[str, Any], *, chave: Any = None, alg: str = "RS256") -> str:
        chave_usada = chave or self.chave
        cabecalho = {"alg": alg, "kid": chave_usada.kid} if alg != "none" else {"alg": alg}
        return jwt.encode(cabecalho, claims, chave_usada)


def _claims(**extras: Any) -> dict[str, Any]:
    agora = int(time.time())
    base: dict[str, Any] = {
        "iss": EMISSOR,
        "sub": "sujeito-1",
        "aud": CLIENTE,
        "exp": agora + 300,
        "iat": agora,
        "nonce": "n-1",
        "email": "Ana@Cliente.com.br",
        "email_verified": True,
        "name": "Ana",
    }
    base.update(extras)
    return {k: v for k, v in base.items() if v is not None}


@pytest.fixture
def servidor() -> Servidor:
    return Servidor()


@pytest.fixture
async def cliente(servidor: Servidor) -> Any:
    async with httpx.AsyncClient(transport=servidor.transporte()) as http:
        yield ClienteOidc(http)


async def test_token_valido(servidor: Servidor, cliente: ClienteOidc) -> None:
    identificacao = await cliente.validar_id_token(_provedor(), servidor.assinar(_claims()), "n-1")
    assert identificacao.sujeito == "sujeito-1"
    assert identificacao.email == "ana@cliente.com.br"
    assert identificacao.nome == "Ana"


@pytest.mark.parametrize(
    "claims",
    [
        _claims(aud="outro-app"),
        _claims(nonce="n-outro"),
        _claims(nonce=None),
        _claims(iss="http://outro.teste/dev"),
        _claims(exp=int(time.time()) - 3600),
        _claims(sub=None),
        _claims(azp="outro-app"),
    ],
    ids=["aud", "nonce", "sem_nonce", "iss", "expirado", "sem_sub", "azp"],
)
async def test_claims_invalidas_sao_recusadas(
    servidor: Servidor, cliente: ClienteOidc, claims: dict[str, Any]
) -> None:
    with pytest.raises(ErroOidc):
        await cliente.validar_id_token(_provedor(), servidor.assinar(claims), "n-1")


async def test_assinatura_de_outra_chave_e_recusada(
    servidor: Servidor, cliente: ClienteOidc
) -> None:
    intrusa = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    with pytest.raises(ErroOidc):
        await cliente.validar_id_token(
            _provedor(), servidor.assinar(_claims(), chave=intrusa), "n-1"
        )


async def test_algoritmo_simetrico_e_recusado(servidor: Servidor, cliente: ClienteOidc) -> None:
    simetrica = OctKey.generate_key(256, parameters={"kid": "k1"})
    with pytest.raises(ErroOidc):
        await cliente.validar_id_token(
            _provedor(), servidor.assinar(_claims(), chave=simetrica, alg="HS256"), "n-1"
        )


async def test_rotacao_de_chave_busca_o_jwks_de_novo(
    servidor: Servidor, cliente: ClienteOidc
) -> None:
    await cliente.validar_id_token(_provedor(), servidor.assinar(_claims()), "n-1")
    servidor.chave = RSAKey.generate_key(2048, parameters={"kid": "k2"})
    identificacao = await cliente.validar_id_token(_provedor(), servidor.assinar(_claims()), "n-1")
    assert identificacao.sujeito == "sujeito-1"
    assert servidor.leituras_jwks == 2


async def test_email_nao_verificado_nao_conta(servidor: Servidor, cliente: ClienteOidc) -> None:
    identificacao = await cliente.validar_id_token(
        _provedor(), servidor.assinar(_claims(email_verified=False)), "n-1"
    )
    assert identificacao.email is None


def test_regras_do_microsoft() -> None:
    ms = _provedor("microsoft")
    assert ms.emissor_valido(EMISSOR_MS, {"tid": TID}, {})
    assert not ms.emissor_valido(EMISSOR_MS, {"tid": "outro"}, {})
    assert not ms.emissor_valido(
        "https://login.microsoftonline.com/outro/v2.0", {"tid": "outro"}, {}
    )
    # E-mail no Microsoft so com xms_edov; email_verified nao basta.
    assert ms.email_verificado({"email": "a@b.com", "xms_edov": True}) == "a@b.com"
    assert ms.email_verificado({"email": "a@b.com", "email_verified": True}) is None
    assert ms.email_verificado({"email": "a@b.com", "xms_edov": "true"}) is None


def test_regras_do_google() -> None:
    google = _provedor("google")
    assert google.emissor_valido("https://accounts.google.com", {}, {})
    assert not google.emissor_valido("https://accounts.google.com.evil", {}, {})
    assert google.email_verificado({"email": "a@b.com", "email_verified": True}) == "a@b.com"
    assert google.email_verificado({"email": "a@b.com"}) is None


async def test_url_de_autorizacao_tem_pkce_e_state(cliente: ClienteOidc) -> None:
    url = await cliente.url_autorizacao(
        _provedor(), redirect_uri="http://app/retorno", state="s", nonce="n", desafio="d"
    )
    assert url.startswith(f"{EMISSOR}/authorize?")
    for parte in ("code_challenge=d", "code_challenge_method=S256", "state=s", "nonce=n"):
        assert parte in url


def _configuracao(**valores: Any) -> Configuracao:
    base: dict[str, Any] = {
        "database_url": "postgresql://a:b@h/d",
        "redis_url": "redis://h",
        "s3_endpoint": "http://h",
        "s3_access_key": "x",
        "s3_secret_key": "y",
        "app_url_base": "https://carbonledger.example",
    }
    base.update(valores)
    return Configuracao(_env_file=None, **base)  # type: ignore[call-arg]


def test_provedor_de_dev_some_sem_emissor_e_e_proibido_em_producao() -> None:
    assert "dev" not in provedores_habilitados(_configuracao())
    assert "dev" in provedores_habilitados(_configuracao(oidc_dev_emissor=EMISSOR))
    with pytest.raises(ValueError, match="desenvolvimento"):
        _configuracao(ambiente="producao", oidc_dev_emissor=EMISSOR)


def test_travas_de_producao() -> None:
    with pytest.raises(ValueError, match="COOKIE_SEGURO"):
        _configuracao(ambiente="producao", cookie_seguro=False)
    with pytest.raises(ValueError, match="https"):
        _configuracao(ambiente="producao", app_url_base="http://carbonledger.example")
    assert _configuracao(ambiente="producao").em_producao
