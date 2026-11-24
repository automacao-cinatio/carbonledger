"""ArmazemS3 contra um MinIO de verdade, com Object Lock: guarda, le, confere o SHA-256 e
recusa apagar a versao retida."""

import hashlib
import time
from collections.abc import Iterator
from typing import Any

import boto3
import httpx
import pytest
from botocore.exceptions import ClientError
from testcontainers.core.container import DockerContainer

from app.core.armazenamento import ArmazemS3
from app.core.config import Configuracao

pytestmark = pytest.mark.integration

IMAGEM_MINIO = "cgr.dev/chainguard/minio:latest"
CHAVE_ACESSO = "minio-teste"
CHAVE_SECRETA = "minio-teste-secreta"
BUCKET = "evidencias-teste"


@pytest.fixture(scope="module")
def minio() -> Iterator[str]:
    conteiner = (
        DockerContainer(IMAGEM_MINIO)
        .with_command("server /data")
        .with_env("MINIO_ROOT_USER", CHAVE_ACESSO)
        .with_env("MINIO_ROOT_PASSWORD", CHAVE_SECRETA)
        .with_exposed_ports(9000)
    )
    with conteiner:
        endpoint = f"http://{conteiner.get_container_host_ip()}:{conteiner.get_exposed_port(9000)}"
        limite = time.monotonic() + 60
        while True:
            try:
                if httpx.get(f"{endpoint}/minio/health/live").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > limite:
                raise RuntimeError("MinIO nao subiu")
            time.sleep(1)
        _cliente(endpoint).create_bucket(Bucket=BUCKET, ObjectLockEnabledForBucket=True)
        yield endpoint


def _cliente(endpoint: str) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=CHAVE_ACESSO,
        aws_secret_access_key=CHAVE_SECRETA,
        region_name="us-east-1",
    )


def _configuracao(endpoint: str) -> Configuracao:
    return Configuracao(
        database_url="postgresql://a:b@h/d",
        redis_url="redis://h",
        s3_endpoint=endpoint,
        s3_access_key=CHAVE_ACESSO,  # type: ignore[arg-type]
        s3_secret_key=CHAVE_SECRETA,  # type: ignore[arg-type]
        s3_bucket_evidencias=BUCKET,
        evidencias_modo_retencao="GOVERNANCE",
        evidencias_retencao_dias=1,
    )


async def test_guarda_le_e_retem(minio: str) -> None:
    armazem = ArmazemS3(_configuracao(minio))
    conteudo = b"%PDF-1.7 evidencia"
    sha256 = hashlib.sha256(conteudo).digest()

    guardado = await armazem.guardar(
        "tenants/t/empresas/e/evidencias/1", conteudo, sha256, "application/pdf"
    )
    assert guardado.versao
    assert await armazem.ler(guardado.chave, guardado.versao) == conteudo

    cliente = _cliente(minio)
    retencao = cliente.get_object_retention(
        Bucket=BUCKET, Key=guardado.chave, VersionId=guardado.versao
    )["Retention"]
    assert retencao["Mode"] == "GOVERNANCE"
    # Apagar a versao retida e recusado (sem o cabecalho de contorno, que o app nunca manda).
    with pytest.raises(ClientError):
        cliente.delete_object(Bucket=BUCKET, Key=guardado.chave, VersionId=guardado.versao)
    assert await armazem.ler(guardado.chave, guardado.versao) == conteudo


async def test_hash_errado_e_recusado_pelo_s3(minio: str) -> None:
    armazem = ArmazemS3(_configuracao(minio))
    with pytest.raises(ClientError):
        await armazem.guardar(
            "tenants/t/empresas/e/evidencias/2",
            b"conteudo",
            hashlib.sha256(b"outro").digest(),
            "text/plain",
        )
