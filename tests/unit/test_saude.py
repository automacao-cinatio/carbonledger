from fastapi.testclient import TestClient

from app.main import app
from app.modules.saude import servico


def _simular(monkeypatch, banco: bool, redis: bool, minio: bool) -> None:  # type: ignore[no-untyped-def]
    async def falso(_config):  # type: ignore[no-untyped-def]
        return {"banco": banco, "redis": redis, "minio": minio}

    monkeypatch.setattr("app.api.saude.verificar_saude", falso)


def test_saude_200_quando_tudo_ok(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _simular(monkeypatch, True, True, True)
    resposta = TestClient(app).get("/saude")
    assert resposta.status_code == 200
    assert resposta.json() == {
        "status": "ok",
        "componentes": {"banco": "ok", "redis": "ok", "minio": "ok"},
    }


def test_saude_503_quando_algum_falha(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _simular(monkeypatch, True, False, True)
    resposta = TestClient(app).get("/saude")
    assert resposta.status_code == 503
    assert resposta.json()["componentes"]["redis"] == "falha"


def test_resposta_traz_request_id(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _simular(monkeypatch, True, True, True)
    resposta = TestClient(app).get("/saude")
    assert len(resposta.headers["X-Request-ID"]) == 32


async def test_verificar_saude_marca_falha_sem_vazar_excecao() -> None:
    from app.core.config import obter_configuracao

    configuracao = obter_configuracao().model_copy(
        update={"database_url": "postgresql://x:y@127.0.0.1:1/z"}
    )
    resultado = await servico.verificar_saude(configuracao)
    assert resultado["banco"] is False
