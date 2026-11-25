"""ProvedorChavesKMS contra um KMS de verdade (local-kms, que implementa a API do AWS KMS):
cifra envelope, contexto amarrado, alias e a configuracao do app com PROVEDOR_CHAVES=kms."""

import time
from collections.abc import Iterator

import boto3
import pytest
from testcontainers.core.container import DockerContainer

from app.core import keys
from app.core.config import Configuracao

pytestmark = pytest.mark.integration

IMAGEM_KMS = "nsmithuk/local-kms:latest"
REGIAO = "sa-east-1"


@pytest.fixture(scope="module")
def kms(monkeypatch_modulo: pytest.MonkeyPatch) -> Iterator[tuple[str, str]]:
    # O local-kms aceita qualquer credencial; o boto3 so exige que exista uma.
    monkeypatch_modulo.setenv("AWS_ACCESS_KEY_ID", "local")
    monkeypatch_modulo.setenv("AWS_SECRET_ACCESS_KEY", "local")
    conteiner = (
        DockerContainer(IMAGEM_KMS)
        .with_env("KMS_REGION", REGIAO)
        .with_env("PORT", "8080")
        .with_exposed_ports(8080)
    )
    with conteiner:
        endpoint = f"http://{conteiner.get_container_host_ip()}:{conteiner.get_exposed_port(8080)}"
        cliente = boto3.client("kms", region_name=REGIAO, endpoint_url=endpoint)
        limite = time.monotonic() + 60
        while True:
            try:
                chave = cliente.create_key(Description="carbonledger-teste")["KeyMetadata"]
                break
            except Exception:
                if time.monotonic() > limite:
                    raise
                time.sleep(1)
        cliente.create_alias(AliasName="alias/carbonledger-teste", TargetKeyId=chave["KeyId"])
        yield endpoint, chave["Arn"]


@pytest.fixture(scope="module")
def monkeypatch_modulo() -> Iterator[pytest.MonkeyPatch]:
    with pytest.MonkeyPatch.context() as patch:
        yield patch


def test_envelope_pelo_kms(kms: tuple[str, str]) -> None:
    endpoint, arn = kms
    provedor = keys.ProvedorChavesKMS.da_configuracao("alias/carbonledger-teste", REGIAO, endpoint)
    contexto = {"tenant_id": "t1", "finalidade": "certificado_assinatura"}
    envelope = keys.cifrar(provedor, b"pfx sem senha", contexto)
    assert keys.decifrar(provedor, envelope, contexto) == b"pfx sem senha"
    # Outro tenant (outro EncryptionContext) nao abre: o proprio KMS recusa.
    with pytest.raises(keys.ErroChave):
        keys.decifrar(provedor, envelope, {**contexto, "tenant_id": "t2"})
    # Pelo ARN tambem abre (o envelope guarda o ARN que cifrou).
    pelo_arn = keys.ProvedorChavesKMS.da_configuracao(arn, REGIAO, endpoint)
    assert keys.decifrar(pelo_arn, envelope, contexto) == b"pfx sem senha"


def test_configuracao_do_app_monta_o_provedor_kms(kms: tuple[str, str]) -> None:
    endpoint, _arn = kms
    configuracao = Configuracao(
        provedor_chaves="kms",
        kms_chave_id="alias/carbonledger-teste",
        kms_regiao=REGIAO,
        kms_endpoint=endpoint,
        chaves_locais=None,
    )
    provedor = keys.provedor_da_configuracao(configuracao)
    assert isinstance(provedor, keys.ProvedorChavesKMS)
    envelope = keys.cifrar(provedor, b"x", {"tenant_id": "t", "finalidade": "f"})
    assert keys.decifrar(provedor, envelope, {"tenant_id": "t", "finalidade": "f"}) == b"x"


def test_chave_inexistente_vira_erro_de_chave(kms: tuple[str, str]) -> None:
    endpoint, _arn = kms
    provedor = keys.ProvedorChavesKMS.da_configuracao("alias/nao-existe", REGIAO, endpoint)
    with pytest.raises(keys.ErroChave, match="cifrar"):
        keys.cifrar(provedor, b"x", {"tenant_id": "t", "finalidade": "f"})
