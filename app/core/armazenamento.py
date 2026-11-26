"""Guarda de objetos no S3 (MinIO com Object Lock em dev).

Cada objeto entra com retencao (Object Lock) e com o SHA-256 conferido pelo proprio S3
(ChecksumSHA256). A chave do objeto so leva identificadores: o nome do arquivo, que pode ter
dado pessoal, fica no banco.

Leitura: pela versao gravada no banco; se essa versao nao existe no bucket (copia restaurada de
backup, que ganha versoes novas), le a versao atual da mesma chave. E seguro porque cada chave
e escrita uma vez so (identificador unico, Object Lock) e quem le sempre confere o SHA-256
registrado antes de usar o conteudo.
"""

import asyncio
import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import boto3
from botocore.config import Config as ConfigBotocore
from botocore.exceptions import ClientError

from app.core.config import Configuracao


@dataclass(frozen=True)
class ObjetoGuardado:
    chave: str
    versao: str | None


class ArmazemObjetos(Protocol):
    async def guardar(
        self, chave: str, conteudo: bytes, sha256: bytes, tipo_conteudo: str
    ) -> ObjetoGuardado: ...

    async def ler(self, chave: str, versao: str | None) -> bytes: ...


# Respostas do S3 (e do MinIO) para versao que nao existe naquele bucket.
CODIGOS_VERSAO_AUSENTE = frozenset({"NoSuchVersion", "InvalidArgument"})


def cliente_s3(configuracao: Configuracao) -> Any:
    """Cliente S3: MinIO com endpoint e chave (dev), ou AWS pela cadeia padrao (papel da tarefa)."""
    opcoes: dict[str, Any] = {}
    estilo = "virtual"
    if configuracao.s3_endpoint:
        # A configuracao recusa endpoint sem chave; aqui so confere de novo.
        if configuracao.s3_access_key is None or configuracao.s3_secret_key is None:
            raise ValueError("S3_ENDPOINT sem S3_ACCESS_KEY e S3_SECRET_KEY")
        opcoes = {
            "endpoint_url": configuracao.s3_endpoint,
            "aws_access_key_id": configuracao.s3_access_key.get_secret_value(),
            "aws_secret_access_key": configuracao.s3_secret_key.get_secret_value(),
        }
        estilo = "path"
    return boto3.client(
        "s3",
        region_name=configuracao.s3_regiao,
        config=ConfigBotocore(
            signature_version="s3v4",
            s3={"addressing_style": estilo},
            connect_timeout=5,
            read_timeout=30,
            retries={"max_attempts": 3},
        ),
        **opcoes,
    )


class ArmazemS3:
    def __init__(self, configuracao: Configuracao) -> None:
        self.bucket = configuracao.s3_bucket_evidencias
        self.modo = configuracao.evidencias_modo_retencao
        self.retencao = timedelta(days=configuracao.evidencias_retencao_dias)
        self._cliente: Any = cliente_s3(configuracao)

    def _guardar(self, chave: str, conteudo: bytes, sha256: bytes, tipo: str) -> ObjetoGuardado:
        resposta = self._cliente.put_object(
            Bucket=self.bucket,
            Key=chave,
            Body=conteudo,
            ContentType=tipo,
            ChecksumSHA256=base64.b64encode(sha256).decode("ascii"),
            ObjectLockMode=self.modo,
            ObjectLockRetainUntilDate=datetime.now(UTC) + self.retencao,
        )
        versao = resposta.get("VersionId")
        return ObjetoGuardado(chave, str(versao) if versao else None)

    def _ler(self, chave: str, versao: str | None) -> bytes:
        argumentos: dict[str, str] = {"Bucket": self.bucket, "Key": chave}
        if versao:
            try:
                resposta = self._cliente.get_object(**argumentos, VersionId=versao)
            except ClientError as erro:
                if erro.response.get("Error", {}).get("Code") not in CODIGOS_VERSAO_AUSENTE:
                    raise
                resposta = self._cliente.get_object(**argumentos)
        else:
            resposta = self._cliente.get_object(**argumentos)
        corpo: bytes = resposta["Body"].read()
        return corpo

    async def guardar(
        self, chave: str, conteudo: bytes, sha256: bytes, tipo_conteudo: str
    ) -> ObjetoGuardado:
        # boto3 e sincrono: roda numa thread para nao travar o event loop da API.
        return await asyncio.to_thread(self._guardar, chave, conteudo, sha256, tipo_conteudo)

    async def ler(self, chave: str, versao: str | None) -> bytes:
        return await asyncio.to_thread(self._ler, chave, versao)
