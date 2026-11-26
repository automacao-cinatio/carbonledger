"""Deploy no ECS (P26): nova revisao das tarefas com a imagem nova, migracao e servicos.

    uv run python scripts/deploy.py --cluster carbonledger-producao \
        --prefixo carbonledger-producao --imagem 1234.dkr.ecr.us-east-1.amazonaws.com/x:SHA \
        --rede-subnets subnet-a,subnet-b --rede-grupos sg-x

Roda no workflow deploy (GitHub Actions, com o papel de deploy assumido por OIDC). Passos:
1. Para cada familia do app, copia a ultima revisao trocando so a imagem do app (variaveis,
   segredos e limites seguem o que o Terraform definiu).
2. Roda a migracao (tarefa avulsa migrar) e para tudo se ela nao sair com 0.
3. Atualiza api, worker e beat e espera estabilizar. Se o ECS reverteu (circuit breaker), falha.

ClamAV nao muda: a imagem dele e de terceiro e so o Terraform troca.
"""

import argparse
import sys
from typing import Any

FAMILIAS_DO_APP = ("migrar", "papeis", "operacao", "api", "worker", "beat")
SERVICOS = ("api", "worker", "beat")
# Campos que describe_task_definition devolve e register_task_definition aceita.
CAMPOS_REGISTRO = (
    "family",
    "taskRoleArn",
    "executionRoleArn",
    "networkMode",
    "containerDefinitions",
    "volumes",
    "placementConstraints",
    "requiresCompatibilities",
    "cpu",
    "memory",
    "runtimePlatform",
    "ephemeralStorage",
)


class FalhaDeploy(Exception):
    """Um passo do deploy falhou; nada depois dele roda."""


def repositorio(imagem: str) -> str:
    """Repositorio de uma imagem (sem a tag nem o digest)."""
    sem_digest = imagem.split("@", 1)[0]
    ultimo = sem_digest.rsplit("/", 1)
    if ":" in ultimo[-1]:
        sem_digest = sem_digest.rsplit(":", 1)[0]
    return sem_digest


def definicao_com_imagem(definicao: dict[str, Any], imagem: str) -> dict[str, Any]:
    """Pedido de registro igual a definicao, com a imagem do app trocada.

    So troca conteiner cuja imagem e do mesmo repositorio: imagem de terceiro fica.
    """
    alvo = repositorio(imagem)
    pedido = {campo: definicao[campo] for campo in CAMPOS_REGISTRO if campo in definicao}
    conteineres = []
    trocados = 0
    for conteiner in definicao["containerDefinitions"]:
        novo = dict(conteiner)
        if repositorio(conteiner["image"]) == alvo:
            novo["image"] = imagem
            trocados += 1
        conteineres.append(novo)
    if trocados == 0:
        raise FalhaDeploy(f"{definicao['family']}: nenhum conteiner do repositorio {alvo}")
    pedido["containerDefinitions"] = conteineres
    return pedido


def registrar(ecs: Any, familia: str, imagem: str) -> str:
    atual = ecs.describe_task_definition(taskDefinition=familia)["taskDefinition"]
    resposta = ecs.register_task_definition(**definicao_com_imagem(atual, imagem))
    return str(resposta["taskDefinition"]["taskDefinitionArn"])


def migrar(ecs: Any, cluster: str, definicao: str, subnets: list[str], grupos: list[str]) -> None:
    resposta = ecs.run_task(
        cluster=cluster,
        taskDefinition=definicao,
        launchType="FARGATE",
        count=1,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": subnets,
                "securityGroups": grupos,
                "assignPublicIp": "DISABLED",
            }
        },
    )
    if resposta.get("failures") or not resposta.get("tasks"):
        raise FalhaDeploy(f"migracao nao iniciou: {resposta.get('failures')}")
    tarefa = resposta["tasks"][0]["taskArn"]
    ecs.get_waiter("tasks_stopped").wait(
        cluster=cluster, tasks=[tarefa], WaiterConfig={"Delay": 10, "MaxAttempts": 90}
    )
    final = ecs.describe_tasks(cluster=cluster, tasks=[tarefa])["tasks"][0]
    codigos = [c.get("exitCode") for c in final.get("containers", [])]
    if codigos != [0]:
        motivo = final.get("stoppedReason", "")
        raise FalhaDeploy(f"migracao falhou (saida {codigos}, {motivo}); veja o log de tarefas")


def atualizar_servicos(ecs: Any, cluster: str, revisoes: dict[str, str]) -> None:
    for servico in SERVICOS:
        ecs.update_service(cluster=cluster, service=servico, taskDefinition=revisoes[servico])
    ecs.get_waiter("services_stable").wait(
        cluster=cluster, services=list(SERVICOS), WaiterConfig={"Delay": 15, "MaxAttempts": 80}
    )
    conferir_servicos(ecs.describe_services(cluster=cluster, services=list(SERVICOS)), revisoes)


def conferir_servicos(resposta: dict[str, Any], revisoes: dict[str, str]) -> None:
    """Estavel nao basta: o circuit breaker pode ter voltado a revisao antiga."""
    for servico in resposta["services"]:
        nome = servico["serviceName"]
        primario = next(d for d in servico["deployments"] if d["status"] == "PRIMARY")
        if primario["taskDefinition"] != revisoes[nome]:
            raise FalhaDeploy(f"{nome}: o ECS reverteu para {primario['taskDefinition']}")
        if primario.get("rolloutState") == "FAILED":
            raise FalhaDeploy(f"{nome}: implantacao falhou ({primario.get('rolloutStateReason')})")


def main(argumentos: list[str] | None = None) -> int:
    analisador = argparse.ArgumentParser(prog="scripts/deploy.py")
    analisador.add_argument("--cluster", required=True)
    analisador.add_argument("--prefixo", required=True, help="prefixo das familias de tarefa")
    analisador.add_argument("--imagem", required=True, help="imagem com a tag do commit")
    analisador.add_argument("--rede-subnets", required=True)
    analisador.add_argument("--rede-grupos", required=True)
    analisador.add_argument("--regiao", default="us-east-1")
    args = analisador.parse_args(argumentos)

    import boto3
    from botocore.exceptions import WaiterError

    ecs = boto3.client("ecs", region_name=args.regiao)
    try:
        revisoes = {
            familia: registrar(ecs, f"{args.prefixo}-{familia}", args.imagem)
            for familia in FAMILIAS_DO_APP
        }
        for familia, arn in revisoes.items():
            print(f"{familia}: {arn}")
        migrar(
            ecs,
            args.cluster,
            revisoes["migrar"],
            args.rede_subnets.split(","),
            args.rede_grupos.split(","),
        )
        print("migracao ok")
        atualizar_servicos(ecs, args.cluster, revisoes)
    except (FalhaDeploy, WaiterError) as erro:
        print(f"deploy interrompido: {erro}", file=sys.stderr)
        return 1
    print("deploy ok: " + ", ".join(SERVICOS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
