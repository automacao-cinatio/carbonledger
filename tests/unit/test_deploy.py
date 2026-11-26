"""Deploy no ECS: troca so a imagem do app, para na migracao que falha e percebe a reversao."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "deploy", Path(__file__).resolve().parents[2] / "scripts" / "deploy.py"
)
assert _SPEC and _SPEC.loader
deploy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(deploy)

REPO = "123456789012.dkr.ecr.us-east-1.amazonaws.com/carbonledger-producao"


def _definicao(familia: str, imagem: str) -> dict[str, Any]:
    return {
        "family": familia,
        "taskDefinitionArn": f"arn:aws:ecs:us-east-1:1:task-definition/{familia}:7",
        "revision": 7,
        "status": "ACTIVE",
        "executionRoleArn": "arn:exec",
        "networkMode": "awsvpc",
        "cpu": "256",
        "memory": "512",
        "requiresCompatibilities": ["FARGATE"],
        "volumes": [{"name": "tmp"}],
        "containerDefinitions": [
            {"name": "app", "image": imagem, "secrets": [{"name": "X", "valueFrom": "arn:s"}]}
        ],
        "registeredAt": "2026-11-25",
    }


def test_repositorio_ignora_tag_digest_e_porta() -> None:
    assert deploy.repositorio(f"{REPO}:abc") == REPO
    assert deploy.repositorio(f"{REPO}@sha256:ff") == REPO
    assert deploy.repositorio("registro:5000/app:1") == "registro:5000/app"
    assert deploy.repositorio("clamav/clamav:1.4") == "clamav/clamav"


def test_troca_so_a_imagem_e_tira_campos_de_leitura() -> None:
    pedido = deploy.definicao_com_imagem(_definicao("f", f"{REPO}:velho"), f"{REPO}:novo")
    assert pedido["containerDefinitions"][0]["image"] == f"{REPO}:novo"
    assert pedido["containerDefinitions"][0]["secrets"] == [{"name": "X", "valueFrom": "arn:s"}]
    for campo in ("taskDefinitionArn", "revision", "status", "registeredAt"):
        assert campo not in pedido
    assert pedido["volumes"] == [{"name": "tmp"}]


def test_imagem_de_terceiro_nao_e_trocada() -> None:
    with pytest.raises(deploy.FalhaDeploy, match="nenhum conteiner"):
        deploy.definicao_com_imagem(_definicao("clamav", "clamav/clamav:1.4"), f"{REPO}:novo")


class EcsFalso:
    def __init__(self, saida_migracao: int | None) -> None:
        self.saida = saida_migracao
        self.chamadas: list[str] = []

    def run_task(self, **_: Any) -> dict[str, Any]:
        self.chamadas.append("run_task")
        return {"tasks": [{"taskArn": "arn:tarefa"}], "failures": []}

    def get_waiter(self, nome: str) -> Any:
        ecs = self

        class Espera:
            def wait(self, **_: Any) -> None:
                ecs.chamadas.append(f"espera:{nome}")

        return Espera()

    def describe_tasks(self, **_: Any) -> dict[str, Any]:
        return {"tasks": [{"containers": [{"exitCode": self.saida}], "stoppedReason": "Essential"}]}


def test_migracao_com_saida_diferente_de_zero_interrompe() -> None:
    with pytest.raises(deploy.FalhaDeploy, match="migracao falhou"):
        deploy.migrar(EcsFalso(1), "c", "arn:migrar", ["s"], ["g"])
    with pytest.raises(deploy.FalhaDeploy, match="migracao falhou"):
        deploy.migrar(EcsFalso(None), "c", "arn:migrar", ["s"], ["g"])


def test_migracao_ok_espera_a_tarefa_parar() -> None:
    ecs = EcsFalso(0)
    deploy.migrar(ecs, "c", "arn:migrar", ["s"], ["g"])
    assert ecs.chamadas == ["run_task", "espera:tasks_stopped"]


def _servico(nome: str, revisao: str, estado: str = "COMPLETED") -> dict[str, Any]:
    return {
        "serviceName": nome,
        "deployments": [
            {"status": "PRIMARY", "taskDefinition": revisao, "rolloutState": estado},
            {"status": "ACTIVE", "taskDefinition": "arn:velha"},
        ],
    }


def test_reversao_do_circuit_breaker_e_falha() -> None:
    revisoes = {"api": "arn:nova-api", "worker": "arn:nova-worker"}
    deploy.conferir_servicos(
        {"services": [_servico("api", "arn:nova-api"), _servico("worker", "arn:nova-worker")]},
        revisoes,
    )
    with pytest.raises(deploy.FalhaDeploy, match="reverteu"):
        deploy.conferir_servicos({"services": [_servico("api", "arn:velha")]}, revisoes)
    with pytest.raises(deploy.FalhaDeploy, match="falhou"):
        deploy.conferir_servicos(
            {"services": [_servico("api", "arn:nova-api", "FAILED")]}, revisoes
        )
