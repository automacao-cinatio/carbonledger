"""Configuracao da aplicacao: tudo vem de variaveis de ambiente."""

from decimal import Decimal
from functools import lru_cache
from typing import Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.db import exige_tls

AMBIENTE_PRODUCAO = "producao"


class Configuracao(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ambiente: str = "desenvolvimento"
    log_nivel: str = "INFO"

    database_url: str
    redis_url: str
    # So a linha de comando de curadoria usa (papel app_curadoria); a API nunca recebe.
    curadoria_database_url: str | None = None

    s3_endpoint: str
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    s3_bucket_evidencias: str = "evidencias"

    # Evidencias: Object Lock no bucket. Em producao, so COMPLIANCE (nem o dono apaga).
    s3_regiao: str = "us-east-1"
    evidencias_modo_retencao: str = "GOVERNANCE"
    evidencias_retencao_dias: int = 1825
    upload_limite_mb: int = 20

    # Paridade com a ferramenta oficial: diferenca maxima por total, em tCO2e. Nunca se
    # afrouxa para um caso passar; mudar exige decisao registrada em docs/decisoes.md.
    paridade_tolerancia_tco2e: Decimal = Decimal("0.001")

    clamav_host: str = "clamav"
    clamav_port: int = 3310
    clamav_tempo_limite_s: float = 60.0

    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    smtp_usuario: str | None = None
    smtp_senha: SecretStr | None = None
    smtp_tls: bool = False
    email_remetente: str = "CarbonLedger <nao-responda@carbonledger.local>"

    # O nome do modelo da Anthropic nunca fica fixo no codigo.
    anthropic_api_key: SecretStr | None = None
    anthropic_modelo: str | None = None
    # Recusa por politica: o servidor repete num modelo de reserva ("default"). Vazio desliga,
    # para modelos que nao aceitam o parametro.
    ia_fallbacks: str = "default"
    ia_tempo_limite_s: float = 30.0

    # Endereco publico do app: base dos links de convite e do retorno do OIDC.
    app_url_base: str = "http://localhost:8000"
    cookie_seguro: bool = True
    sessao_inatividade_min: int = 120
    sessao_maxima_h: int = 12
    convite_validade_dias: int = 7
    # Limite de inicios de login por IP numa janela (protege contra tentativa em massa).
    login_limite_tentativas: int = 20
    login_janela_min: int = 10

    oidc_google_client_id: str | None = None
    oidc_google_client_secret: SecretStr | None = None
    oidc_microsoft_client_id: str | None = None
    oidc_microsoft_client_secret: SecretStr | None = None
    # Provedor simulado (mock-oauth2-server), so fora de producao.
    oidc_dev_emissor: str | None = None
    oidc_dev_url_publica: str | None = None
    oidc_dev_client_id: str = "carbonledger-dev"
    oidc_dev_client_secret: SecretStr = SecretStr("dev")

    # Chaves mestras: "local" (variavel de ambiente) em dev; "kms" em producao (P25).
    provedor_chaves: str = "local"
    chaves_locais: SecretStr | None = None
    # KMS (PROVEDOR_CHAVES=kms): ARN, id ou alias da chave mestra. Credenciais pela cadeia
    # padrao da AWS (papel da instancia em producao). KMS_ENDPOINT so em dev (local-kms).
    kms_chave_id: str | None = None
    kms_regiao: str = "sa-east-1"
    kms_endpoint: str | None = None

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
            if self.provedor_chaves != "kms" or self.chaves_locais is not None:
                raise ValueError("Em producao as chaves vem do KMS: PROVEDOR_CHAVES=kms.")
            if not self.kms_chave_id:
                raise ValueError("Em producao defina KMS_CHAVE_ID.")
            if not exige_tls(self.database_url):
                raise ValueError("Em producao a DATABASE_URL precisa de sslmode=require (TLS).")
            if self.evidencias_modo_retencao != "COMPLIANCE":
                raise ValueError("Em producao EVIDENCIAS_MODO_RETENCAO precisa ser COMPLIANCE.")
        if self.provedor_chaves not in ("local", "kms"):
            raise ValueError("PROVEDOR_CHAVES aceita local ou kms.")
        if self.provedor_chaves == "kms" and not self.kms_chave_id:
            raise ValueError("Com PROVEDOR_CHAVES=kms, defina KMS_CHAVE_ID.")
        if self.evidencias_modo_retencao not in ("GOVERNANCE", "COMPLIANCE"):
            raise ValueError("EVIDENCIAS_MODO_RETENCAO aceita GOVERNANCE ou COMPLIANCE.")
        if self.evidencias_retencao_dias < 1 or not 1 <= self.upload_limite_mb <= 100:
            raise ValueError("Retencao minima de 1 dia; limite de upload entre 1 e 100 MB.")
        return self


@lru_cache
def obter_configuracao() -> Configuracao:
    return Configuracao()
