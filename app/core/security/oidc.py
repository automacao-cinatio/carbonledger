"""Login por OIDC (fluxo de codigo com PKCE) com Google, Microsoft e o provedor de dev.

Validacao do id_token: assinatura RS256 pelas chaves publicadas (JWKS), emissor pela regra
de cada provedor, audiencia, validade, nonce e azp. Qualquer falha vira ErroOidc, e a rota
responde 404 sem dizer o motivo.

E-mail so conta quando o provedor garante a posse: email_verified no Google e no dev,
xms_edov no Microsoft (claim opcional que precisa estar ligada no registro do app no Entra).
"""

import base64
import hashlib
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from pydantic import SecretStr

from app.core.config import Configuracao

ESCOPOS = "openid email profile"
ALGORITMOS = ["RS256"]
FOLGA_RELOGIO_S = 60
VALIDADE_CACHE_S = 3600

EMISSORES_GOOGLE = frozenset({"https://accounts.google.com", "accounts.google.com"})
_TID_MICROSOFT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class ErroOidc(Exception):
    """Falha no login OIDC. O detalhe fica so no codigo; o usuario ve 404."""


@dataclass(frozen=True)
class Provedor:
    nome: str
    descoberta_url: str
    client_id: str
    client_secret: SecretStr
    # Endereco que o navegador usa, quando difere do que o servidor usa (so no dev).
    url_publica: str | None = None

    def emissor_valido(
        self, emissor: str, claims: dict[str, Any], descoberta: dict[str, Any]
    ) -> bool:
        if self.nome == "google":
            return emissor in EMISSORES_GOOGLE
        if self.nome == "microsoft":
            tid = str(claims.get("tid", ""))
            return bool(_TID_MICROSOFT.fullmatch(tid)) and (
                emissor == f"https://login.microsoftonline.com/{tid}/v2.0"
            )
        return emissor == descoberta.get("issuer")

    def email_verificado(self, claims: dict[str, Any]) -> str | None:
        email = claims.get("email")
        if not isinstance(email, str) or "@" not in email:
            return None
        garantido = (
            claims.get("xms_edov") if self.nome == "microsoft" else claims.get("email_verified")
        )
        return email.strip().lower() if garantido is True else None


@dataclass(frozen=True)
class Identificacao:
    provedor: str
    sujeito: str
    email: str | None
    nome: str


def provedores_habilitados(configuracao: Configuracao) -> dict[str, Provedor]:
    provedores: dict[str, Provedor] = {}
    if configuracao.oidc_google_client_id and configuracao.oidc_google_client_secret:
        provedores["google"] = Provedor(
            nome="google",
            descoberta_url="https://accounts.google.com/.well-known/openid-configuration",
            client_id=configuracao.oidc_google_client_id,
            client_secret=configuracao.oidc_google_client_secret,
        )
    if configuracao.oidc_microsoft_client_id and configuracao.oidc_microsoft_client_secret:
        # "organizations": so contas corporativas ou de escola, nunca contas pessoais.
        provedores["microsoft"] = Provedor(
            nome="microsoft",
            descoberta_url="https://login.microsoftonline.com/organizations/v2.0/.well-known/openid-configuration",
            client_id=configuracao.oidc_microsoft_client_id,
            client_secret=configuracao.oidc_microsoft_client_secret,
        )
    if configuracao.oidc_dev_emissor and not configuracao.em_producao:
        emissor = configuracao.oidc_dev_emissor.rstrip("/")
        provedores["dev"] = Provedor(
            nome="dev",
            descoberta_url=f"{emissor}/.well-known/openid-configuration",
            client_id=configuracao.oidc_dev_client_id,
            client_secret=configuracao.oidc_dev_client_secret,
            url_publica=configuracao.oidc_dev_url_publica,
        )
    return provedores


def _b64url(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).rstrip(b"=").decode("ascii")


def novo_verificador_pkce() -> tuple[str, str]:
    """Par (verificador, desafio S256) do PKCE."""
    verificador = _b64url(secrets.token_bytes(48))
    return verificador, _b64url(hashlib.sha256(verificador.encode("ascii")).digest())


@dataclass
class _Cache:
    valor: Any
    expira: float


@dataclass
class ClienteOidc:
    http: httpx.AsyncClient
    _descobertas: dict[str, _Cache] = field(default_factory=dict)
    _chaves: dict[str, _Cache] = field(default_factory=dict)

    async def _json(self, url: str) -> dict[str, Any]:
        try:
            resposta = await self.http.get(url)
            resposta.raise_for_status()
            dados = resposta.json()
        except (httpx.HTTPError, ValueError) as erro:
            raise ErroOidc(f"falha ao ler {url}") from erro
        if not isinstance(dados, dict):
            raise ErroOidc(f"resposta inesperada de {url}")
        return dados

    async def descoberta(self, provedor: Provedor) -> dict[str, Any]:
        cache = self._descobertas.get(provedor.nome)
        if cache is None or cache.expira < time.monotonic():
            dados = await self._json(provedor.descoberta_url)
            cache = _Cache(dados, time.monotonic() + VALIDADE_CACHE_S)
            self._descobertas[provedor.nome] = cache
        valor: dict[str, Any] = cache.valor
        return valor

    async def _conjunto_de_chaves(self, provedor: Provedor, *, renovar: bool) -> KeySet:
        cache = self._chaves.get(provedor.nome)
        if renovar or cache is None or cache.expira < time.monotonic():
            descoberta = await self.descoberta(provedor)
            dados = await self._json(str(descoberta["jwks_uri"]))
            cache = _Cache(KeySet.import_key_set(dados), time.monotonic() + VALIDADE_CACHE_S)  # type: ignore[arg-type]
            self._chaves[provedor.nome] = cache
        chaves: KeySet = cache.valor
        return chaves

    async def url_autorizacao(
        self, provedor: Provedor, *, redirect_uri: str, state: str, nonce: str, desafio: str
    ) -> str:
        descoberta = await self.descoberta(provedor)
        endpoint = str(descoberta["authorization_endpoint"])
        if provedor.url_publica:
            emissor = str(descoberta["issuer"]).rstrip("/")
            endpoint = endpoint.replace(emissor, provedor.url_publica.rstrip("/"), 1)
        parametros = {
            "response_type": "code",
            "client_id": provedor.client_id,
            "redirect_uri": redirect_uri,
            "scope": ESCOPOS,
            "state": state,
            "nonce": nonce,
            "code_challenge": desafio,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        return f"{endpoint}?{urlencode(parametros)}"

    async def trocar_codigo(
        self, provedor: Provedor, *, codigo: str, redirect_uri: str, verificador: str
    ) -> str:
        descoberta = await self.descoberta(provedor)
        try:
            resposta = await self.http.post(
                str(descoberta["token_endpoint"]),
                data={
                    "grant_type": "authorization_code",
                    "code": codigo,
                    "redirect_uri": redirect_uri,
                    "client_id": provedor.client_id,
                    "client_secret": provedor.client_secret.get_secret_value(),
                    "code_verifier": verificador,
                },
                headers={"Accept": "application/json"},
            )
            resposta.raise_for_status()
            id_token = resposta.json().get("id_token")
        except (httpx.HTTPError, ValueError, AttributeError) as erro:
            raise ErroOidc("falha na troca do codigo") from erro
        if not isinstance(id_token, str):
            raise ErroOidc("resposta sem id_token")
        return id_token

    async def validar_id_token(
        self, provedor: Provedor, id_token: str, nonce: str, *, agora: int | None = None
    ) -> Identificacao:
        descoberta = await self.descoberta(provedor)
        try:
            token = await self._decodificar(provedor, id_token)
        except JoseError as erro:
            raise ErroOidc("assinatura do id_token invalida") from erro
        claims: dict[str, Any] = dict(token.claims)

        registro = jwt.JWTClaimsRegistry(
            now=agora,
            leeway=FOLGA_RELOGIO_S,
            iss={"essential": True},
            sub={"essential": True},
            aud={"essential": True, "value": provedor.client_id},
            exp={"essential": True},
            iat={"essential": True},
            nonce={"essential": True, "value": nonce},
        )
        try:
            registro.validate(claims)
        except JoseError as erro:
            raise ErroOidc(f"claims invalidas: {type(erro).__name__}") from erro
        if not provedor.emissor_valido(str(claims["iss"]), claims, descoberta):
            raise ErroOidc("emissor invalido")
        if "azp" in claims and claims["azp"] != provedor.client_id:
            raise ErroOidc("azp invalido")

        email = provedor.email_verificado(claims)
        nome = claims.get("name")
        return Identificacao(
            provedor=provedor.nome,
            sujeito=str(claims["sub"]),
            email=email,
            nome=nome.strip() if isinstance(nome, str) and nome.strip() else (email or "Sem nome"),
        )

    async def _decodificar(self, provedor: Provedor, id_token: str) -> jwt.Token:
        try:
            return jwt.decode(
                id_token, await self._conjunto_de_chaves(provedor, renovar=False), ALGORITMOS
            )
        except JoseError:
            # Chave nova (rotacao): busca o JWKS de novo uma unica vez.
            return jwt.decode(
                id_token, await self._conjunto_de_chaves(provedor, renovar=True), ALGORITMOS
            )
