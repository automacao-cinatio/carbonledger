"""Configuracao da aplicacao: tudo vem de variaveis de ambiente."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Configuracao(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ambiente: str = "desenvolvimento"
    log_nivel: str = "INFO"

    database_url: str
    redis_url: str

    s3_endpoint: str
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    s3_bucket_evidencias: str = "evidencias"

    clamav_host: str = "clamav"
    clamav_port: int = 3310

    smtp_host: str = "mailpit"
    smtp_port: int = 1025

    # O nome do modelo da Anthropic nunca fica fixo no codigo.
    anthropic_api_key: SecretStr | None = None
    anthropic_modelo: str | None = None


@lru_cache
def obter_configuracao() -> Configuracao:
    return Configuracao()
