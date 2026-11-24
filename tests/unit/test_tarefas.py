from typing import Any
from uuid import UUID

import pytest

from app.core.tenancy import Escopo
from app.workers.celery_app import celery_app
from app.workers.registro import (
    TAREFAS_DE_TENANT,
    TAREFAS_GLOBAIS,
    TarefaSemTenant,
    eh_global,
    exigir_tenant,
)
from app.workers.tarefas import contexto_da_tarefa, tarefa_de_tenant

T = "11111111-1111-4111-8111-111111111111"
E1 = "22222222-2222-4222-8222-222222222222"


@tarefa_de_tenant("teste_unitario.eco")
async def _eco(_conexao: Any, _contexto: Any, **kwargs: Any) -> dict[str, Any]:
    return kwargs


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        (None, None),
        ((), {}),
        ((T,), {}),
        ((), {"outro": 1}),
        ((), {"tenant_id": None}),
        ((), {"tenant_id": "nao-e-uuid"}),
    ],
)
def test_publicacao_sem_tenant_e_recusada(args: Any, kwargs: Any) -> None:
    with pytest.raises(TarefaSemTenant):
        exigir_tenant("modulo.tarefa", args, kwargs)


def test_publicacao_com_tenant_passa() -> None:
    exigir_tenant("modulo.tarefa", (), {"tenant_id": T, "planilha_id": "x"})


def test_tarefas_internas_do_celery_sao_globais() -> None:
    assert eh_global("celery.ping")
    exigir_tenant("celery.ping", None, None)


def test_send_task_recusa_antes_de_tocar_o_broker() -> None:
    # Sem tenant, o erro sai antes de qualquer conexao com o Redis.
    with pytest.raises(TarefaSemTenant):
        celery_app.send_task("modulo.tarefa", kwargs={"planilha_id": "x"})
    with pytest.raises(TarefaSemTenant):
        _eco.delay(planilha_id="x")
    with pytest.raises(TarefaSemTenant):
        _eco.apply_async(args=(T,))


def test_contexto_da_tarefa_tira_os_argumentos_reservados() -> None:
    kwargs: dict[str, Any] = {"tenant_id": T, "empresas": [E1], "planilha_id": "p"}
    contexto = contexto_da_tarefa("t", kwargs)
    assert contexto.tenant_id == UUID(T)
    assert contexto.escopo is Escopo.EMPRESAS
    assert contexto.empresas == frozenset({UUID(E1)})
    assert kwargs == {"planilha_id": "p"}


def test_sem_empresas_o_escopo_e_o_tenant() -> None:
    contexto = contexto_da_tarefa("t", {"tenant_id": T})
    assert contexto.escopo is Escopo.TENANT
    assert contexto.empresas == frozenset()


def test_lista_vazia_de_empresas_nao_vira_tenant_inteiro() -> None:
    contexto = contexto_da_tarefa("t", {"tenant_id": T, "empresas": []})
    assert contexto.escopo is Escopo.EMPRESAS
    assert contexto.empresas == frozenset()


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"tenant_id": "x"},
        {"tenant_id": T, "empresas": ["x"]},
        {"tenant_id": T, "usuario_id": "x"},
    ],
)
def test_contexto_da_tarefa_invalido(kwargs: dict[str, Any]) -> None:
    with pytest.raises(TarefaSemTenant):
        contexto_da_tarefa("t", kwargs)


def test_execucao_sem_tenant_e_recusada_antes_do_banco() -> None:
    with pytest.raises(TarefaSemTenant):
        _eco(planilha_id="x")
    with pytest.raises(TarefaSemTenant):
        _eco(T)


def test_nome_global_nao_vira_tarefa_de_tenant() -> None:
    with pytest.raises(ValueError, match="globais"):
        tarefa_de_tenant("celery.qualquer")


def test_toda_tarefa_registrada_e_de_tenant_ou_global_justificada() -> None:
    celery_app.loader.import_default_modules()
    for nome in celery_app.tasks:
        if nome.startswith("celery."):
            continue
        assert nome in TAREFAS_DE_TENANT or nome in TAREFAS_GLOBAIS, (
            f"{nome}: use tarefa_de_tenant ou registre em TAREFAS_GLOBAIS com justificativa"
        )
    for nome, justificativa in TAREFAS_GLOBAIS.items():
        assert justificativa.strip(), f"{nome} sem justificativa"


def test_disparo_da_integridade_enfileira_um_por_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.workers import integridade

    tenants = ["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"]

    async def falsos() -> list[str]:
        return tenants

    enfileirados: list[dict[str, str]] = []
    monkeypatch.setattr(integridade, "_tenants", falsos)
    monkeypatch.setattr(
        integridade.verificar_tenant, "delay", lambda **kwargs: enfileirados.append(kwargs)
    )
    assert integridade.disparar() == 2
    assert enfileirados == [{"tenant_id": t} for t in tenants]
    agenda = integridade.celery_app.conf.beat_schedule["integridade-diaria"]
    assert agenda["task"] == "integridade.disparar"


def test_disparo_das_campanhas_enfileira_um_por_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.workers import campanhas

    tenant = "33333333-3333-4333-8333-333333333333"

    async def falsos() -> list[str]:
        return [tenant]

    enfileirados: list[dict[str, str]] = []
    monkeypatch.setattr(campanhas, "_tenants", falsos)
    monkeypatch.setattr(
        campanhas.lembrar_tenant, "delay", lambda **kwargs: enfileirados.append(kwargs)
    )
    assert campanhas.disparar() == 1
    assert enfileirados == [{"tenant_id": tenant}]
    assert campanhas.celery_app.conf.beat_schedule["campanhas-diaria"]["task"] == (
        "campanhas.disparar"
    )
    # As duas rotinas diarias convivem na agenda do beat.
    assert "integridade-diaria" in campanhas.celery_app.conf.beat_schedule
