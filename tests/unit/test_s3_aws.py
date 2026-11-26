"""S3 na AWS: sem endpoint nem chave (papel da tarefa); MinIO com endpoint e chave."""

from typing import Any

import pytest

from app.core.armazenamento import cliente_s3
from app.modules.saude import servico
from tests.unit.test_oidc import PRODUCAO, _configuracao

AWS = {"s3_endpoint": None, "s3_access_key": None, "s3_secret_key": None}


def test_aws_sem_endpoint_e_sem_chave_vale_em_producao() -> None:
    configuracao = _configuracao(**PRODUCAO, **AWS)
    assert configuracao.s3_endpoint is None


def test_chave_sem_endpoint_e_recusada() -> None:
    with pytest.raises(ValueError, match="papel da tarefa"):
        _configuracao(**{**AWS, "s3_access_key": "x"})


def test_endpoint_sem_chave_e_recusado() -> None:
    with pytest.raises(ValueError, match="S3_ACCESS_KEY"):
        _configuracao(**{**AWS, "s3_endpoint": "http://minio:9000"})


def test_cliente_aws_usa_endereco_padrao_e_estilo_virtual(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sem credenciais no ambiente do teste: o cliente nasce assim mesmo (so assina ao chamar).
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "teste")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "teste")
    cliente = cliente_s3(_configuracao(**AWS, s3_regiao="us-east-1"))
    assert cliente.meta.endpoint_url == "https://s3.amazonaws.com"
    assert cliente.meta.config.s3["addressing_style"] == "virtual"


def test_cliente_minio_usa_endpoint_e_estilo_de_caminho() -> None:
    cliente = cliente_s3(_configuracao(s3_endpoint="http://minio:9000"))
    assert cliente.meta.endpoint_url == "http://minio:9000"
    assert cliente.meta.config.s3["addressing_style"] == "path"


async def test_saude_na_aws_pergunta_pelo_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    chamadas: list[dict[str, Any]] = []

    class ClienteFalso:
        def head_bucket(self, **argumentos: Any) -> None:
            chamadas.append(argumentos)

    monkeypatch.setattr("app.core.armazenamento.cliente_s3", lambda _c: ClienteFalso())
    configuracao = _configuracao(**AWS, s3_bucket_evidencias="carbonledger-evidencias")
    await servico.checar_armazenamento(configuracao)
    assert chamadas == [{"Bucket": "carbonledger-evidencias"}]


async def test_saude_com_minio_nao_chama_o_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    vistos: list[str] = []

    async def minio(endpoint: str) -> None:
        vistos.append(endpoint)

    monkeypatch.setattr(servico, "checar_minio", minio)
    await servico.checar_armazenamento(_configuracao(s3_endpoint="http://minio:9000"))
    assert vistos == ["http://minio:9000"]
