"""Portal de ponta a ponta nos testes: app real, Redis, provedor OIDC simulado e login.

Os fixtures (redis_url, emissor_oidc, emails, app_cliente) ficam no conftest; aqui ficam as
pecas que os modulos de teste importam.
"""

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy import text
from testcontainers.community.redis import RedisContainer
from testcontainers.core.container import DockerContainer

from app.api.dependencias import SessaoTenant
from app.core.antivirus import AntivirusIndisponivel, ArquivoInfectado
from app.core.armazenamento import ObjetoGuardado

IMAGEM_OIDC = "ghcr.io/navikt/mock-oauth2-server:latest"
COOKIE_SESSAO = "cl_sessao"

rotas_teste = APIRouter(prefix="/teste-login")


@rotas_teste.get("/empresas")
async def empresas_visiveis(sessao: SessaoTenant) -> list[str]:
    linhas = await sessao.execute(text("SELECT nome FROM empresa ORDER BY nome"))
    return list(linhas.scalars())


class ArmazemMemoria:
    """S3 de mentira para o portal de teste: guarda em memoria e nunca deixa sobrescrever.

    O S3 de verdade (MinIO com Object Lock) e exercitado em test_armazenamento_minio.py.
    """

    def __init__(self) -> None:
        self.objetos: dict[str, tuple[bytes, str]] = {}

    async def guardar(
        self, chave: str, conteudo: bytes, sha256: bytes, tipo_conteudo: str
    ) -> ObjetoGuardado:
        assert chave not in self.objetos, "objeto sobrescrito"
        self.objetos[chave] = (conteudo, tipo_conteudo)
        return ObjetoGuardado(chave, "v1")

    async def ler(self, chave: str, versao: str | None) -> bytes:
        return self.objetos[chave][0]


class AntivirusFalso:
    """ClamAV de mentira: acusa a assinatura EICAR; fora_do_ar simula o ClamAV parado."""

    def __init__(self) -> None:
        self.fora_do_ar = False
        self.verificados = 0

    async def verificar(self, conteudo: bytes) -> None:
        if self.fora_do_ar:
            raise AntivirusIndisponivel("simulado")
        self.verificados += 1
        if b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in conteudo:
            raise ArquivoInfectado


PASTA_FICTICIOS = Path(__file__).resolve().parents[1] / "dados_ficticios"
CONJUNTO_GWP_DE_TESTE = "TESTE_CALC_100"


async def carregar_referencia_de_calculo(dsn_curadoria: str) -> None:
    """Importa e aprova os fatores, misturas e GWP ficticios do calculo (uma vez por banco)."""
    import asyncpg

    from app.modules.fatores import repositorio

    conexao = await asyncpg.connect(dsn_curadoria)
    try:
        for tipo, arquivo in (
            ("fatores", "calculo_fatores.csv"),
            ("misturas", "calculo_misturas.csv"),
            ("gwp", "calculo_gwp.csv"),
        ):
            caminho = PASTA_FICTICIOS / arquivo
            try:
                lote = await repositorio.importar(
                    conexao, tipo, arquivo, caminho.read_bytes(), "Teste do calculo"
                )
            except repositorio.LoteRepetido:
                continue  # outro teste da sessao ja carregou e aprovou
            await repositorio.decidir(
                conexao, lote.lote_id, aprovar=True, responsavel="Teste do calculo"
            )
    finally:
        await conexao.close()


def subir_redis() -> Iterator[str]:
    with RedisContainer("redis:7") as conteiner:
        host = conteiner.get_container_host_ip()
        yield f"redis://{host}:{conteiner.get_exposed_port(6379)}/0"


def subir_oidc() -> Iterator[str]:
    conteiner = (
        DockerContainer(IMAGEM_OIDC)
        .with_exposed_ports(8080)
        .with_env("JSON_CONFIG", '{"interactiveLogin": true}')
    )
    with conteiner:
        emissor = (
            f"http://{conteiner.get_container_host_ip()}:{conteiner.get_exposed_port(8080)}/dev"
        )
        limite = time.monotonic() + 90
        while True:
            try:
                if httpx.get(f"{emissor}/.well-known/openid-configuration").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > limite:
                raise RuntimeError("provedor OIDC simulado nao subiu")
            time.sleep(1)
        yield emissor


def entrar(
    cliente: TestClient,
    inicio: str,
    *,
    sujeito: str,
    email: str | None,
    verificado: bool = True,
) -> httpx.Response:
    """Percorre o fluxo inteiro e devolve a resposta do retorno no app."""
    cliente.cookies.clear()
    ida = cliente.get(inicio)
    assert ida.status_code == 303, ida.text
    claims: dict[str, Any] = {"email_verified": verificado, "name": sujeito.title()}
    if email is not None:
        claims["email"] = email
    no_provedor = httpx.post(
        ida.headers["location"],
        data={"username": sujeito, "claims": json.dumps(claims)},
        follow_redirects=False,
    )
    assert no_provedor.status_code == 302, no_provedor.text
    volta = urlsplit(no_provedor.headers["location"])
    return cliente.get(f"{volta.path}?{volta.query}")


def csrf(cliente: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": cliente.get("/sessao").json()["csrf"]}
