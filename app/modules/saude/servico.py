"""Verificacoes de saude das dependencias: banco, Redis e armazenamento (MinIO ou S3)."""

import asyncio
from collections.abc import Awaitable, Callable

import asyncpg
import httpx
import redis.asyncio as redis_async
import structlog

from app.core.config import Configuracao
from app.core.db import dsn_asyncpg

TEMPO_LIMITE_S = 3.0

log = structlog.get_logger()


async def checar_banco(database_url: str) -> None:
    conexao = await asyncpg.connect(dsn_asyncpg(database_url), timeout=TEMPO_LIMITE_S)
    try:
        await conexao.execute("SELECT 1")
    finally:
        await conexao.close()


async def checar_redis(redis_url: str) -> None:
    cliente = redis_async.from_url(redis_url, socket_timeout=TEMPO_LIMITE_S)
    try:
        await cliente.ping()
    finally:
        await cliente.aclose()


async def checar_minio(endpoint: str) -> None:
    async with httpx.AsyncClient(timeout=TEMPO_LIMITE_S) as cliente:
        resposta = await cliente.get(f"{endpoint.rstrip('/')}/minio/health/live")
        resposta.raise_for_status()


async def checar_s3(configuracao: Configuracao) -> None:
    """Na AWS: o bucket de evidencias responde ao papel da tarefa (HeadBucket)."""
    from app.core.armazenamento import cliente_s3

    cliente = cliente_s3(configuracao)
    await asyncio.to_thread(cliente.head_bucket, Bucket=configuracao.s3_bucket_evidencias)


def checar_armazenamento(configuracao: Configuracao) -> Awaitable[None]:
    if configuracao.s3_endpoint:
        return checar_minio(configuracao.s3_endpoint)
    return checar_s3(configuracao)


async def _executar(nome: str, verificacao: Callable[[], Awaitable[None]]) -> tuple[str, bool]:
    try:
        await asyncio.wait_for(verificacao(), timeout=TEMPO_LIMITE_S + 1)
    except Exception as erro:
        # So o tipo do erro vai para o log: mensagens de driver podem trazer a DSN.
        log.warning("saude_falha", componente=nome, erro=type(erro).__name__)
        return nome, False
    return nome, True


async def verificar_saude(configuracao: Configuracao) -> dict[str, bool]:
    resultados = await asyncio.gather(
        _executar("banco", lambda: checar_banco(configuracao.database_url)),
        _executar("redis", lambda: checar_redis(configuracao.redis_url)),
        _executar("armazenamento", lambda: checar_armazenamento(configuracao)),
    )
    return dict(resultados)
