"""Configuracao da aplicacao: tudo vem de variaveis de ambiente."""

from functools import lru_cache
from typing import Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AMBIENTE_PRODUCAO = "producao"


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
    smtp_usuario: str | None = None
    smtp_senha: SecretStr | None = None
    smtp_tls: bool = False
    email_remetente: str = "CarbonLedger <nao-responda@carbonledger.local>"

    # O nome do modelo da Anthropic nunca fica fixo no codigo.
    anthropic_api_key: SecretStr | None = None
    anthropic_modelo: str | None = None

    # Endereco publico do app: base dos links de convite e do retorno do OIDC.
    app_url_base: str = "http://localhost:8000"
    cookie_seguro: bool = True
    sessao_inatividade_min: int = 120
    sessao_maxima_h: int = 12
    convite_validade_dias: int = 7

    oidc_google_client_id: str | None = None
    oidc_google_client_secret: SecretStr | None = None
    oidc_microsoft_client_id: str | None = None
    oidc_microsoft_client_secret: SecretStr | None = None
    # Provedor simulado (mock-oauth2-server), so fora de producao.
    oidc_dev_emissor: str | None = None
    oidc_dev_url_publica: str | None = None
    oidc_dev_client_id: str = "carbonledger-dev"
    oidc_dev_client_secret: SecretStr = SecretStr("dev")

    @property
    def em_producao(self) -> bool:
        return self.ambiente == AMBIENTE_PRODUCAO

    @model_validator(mode="after")
    def _travas_de_producao(self) -> Self:
        if self.em_producao:
            if not self.cookie_seguro:
                raise ValueError("Em producao COOKIE_SEGURO precisa ser true.")
            if self.oidc_dev_emissor:
                raise ValueError("O provedor OIDC de desenvolvimento nao pode existir em producao.")
            if not self.app_url_base.startswith("https://"):
                raise ValueError("Em producao APP_URL_BASE precisa usar https.")
        return self


@lru_cache
def obter_configuracao() -> Configuracao:
    return Configuracao()
