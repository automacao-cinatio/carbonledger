from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.audit import (
    HASH_INICIAL,
    EventoInvalido,
    LinhaTrilha,
    conteudo_v1,
    hash_da_linha,
    preparar_evento,
    verificar_cadeia,
)

TENANT = uuid4()


def _cadeia(tamanho: int) -> list[LinhaTrilha]:
    linhas: list[LinhaTrilha] = []
    anterior = HASH_INICIAL
    inicio = datetime(2026, 11, 20, 12, 0, 0, 123456, tzinfo=UTC)
    for seq in range(1, tamanho + 1):
        rascunho = LinhaTrilha(
            tenant_id=TENANT,
            seq=seq,
            evento_id=uuid4(),
            ocorrido_em=inicio + timedelta(seconds=seq),
            usuario_id=None if seq % 2 else uuid4(),
            acao="convite.criado",
            entidade="convite",
            entidade_id=str(uuid4()),
            dados_texto='{"perfil": "gestor"}',
            hash_anterior=anterior,
            hash=b"",
        )
        linha = replace(rascunho, hash=hash_da_linha(rascunho))
        linhas.append(linha)
        anterior = linha.hash
    return linhas


def test_cadeia_integra() -> None:
    resultado = verificar_cadeia(_cadeia(5))
    assert resultado.integra
    assert resultado.eventos == 5


def test_cadeia_vazia_e_integra() -> None:
    assert verificar_cadeia([]).integra


@pytest.mark.parametrize(
    ("alteracao", "motivo"),
    [
        (
            lambda c: c.__setitem__(2, replace(c[2], dados_texto='{"perfil": "administrador"}')),
            "conteudo alterado",
        ),
        (lambda c: c.__setitem__(2, replace(c[2], acao="convite.revogado")), "conteudo alterado"),
        (lambda c: c.pop(2), "sequencia com falha"),
        (
            lambda c: c.__setitem__(2, replace(c[2], hash_anterior=bytes(32))),
            "encadeamento rompido",
        ),
    ],
    ids=["dados", "acao", "evento_removido", "elo_trocado"],
)
def test_adulteracao_e_detectada(alteracao, motivo: str) -> None:  # type: ignore[no-untyped-def]
    cadeia = _cadeia(5)
    alteracao(cadeia)
    resultado = verificar_cadeia(cadeia)
    assert not resultado.integra
    # Evento removido: a quebra aparece no primeiro que ficou fora de sequencia (o 4).
    assert resultado.quebra_em == (4 if motivo == "sequencia com falha" else 3)
    assert resultado.motivo == motivo


def test_recalcular_tudo_depois_de_adulterar_ainda_quebra_no_elo_seguinte() -> None:
    """Quem altera um evento e recalcula o hash dele rompe o encadeamento do proximo."""
    cadeia = _cadeia(4)
    alterada = replace(cadeia[1], dados_texto='{"perfil": "administrador"}')
    cadeia[1] = replace(alterada, hash=hash_da_linha(alterada))
    resultado = verificar_cadeia(cadeia)
    assert (resultado.integra, resultado.quebra_em) == (False, 3)


def test_conteudo_v1_tem_formato_fixo() -> None:
    linha = _cadeia(1)[0]
    partes = conteudo_v1(linha).decode().split("|")
    assert partes[0] == "v1"
    assert partes[4] == "2026-11-20T12:00:01.123456Z"
    assert partes[5] == ""


@pytest.mark.parametrize(
    "dados",
    [
        {"email": "a@b.com"},
        {"Nome": "Ana"},
        {"alvo": {"cpf": "123"}},
        {"lista": [{"telefone": "1"}]},
        {"objeto": object()},
    ],
)
def test_dado_pessoal_ou_tipo_estranho_e_recusado(dados: dict[str, object]) -> None:
    with pytest.raises(EventoInvalido):
        preparar_evento(acao="membro.alterado", entidade="membro", entidade_id=None, dados=dados)


@pytest.mark.parametrize(
    ("acao", "entidade", "entidade_id"),
    [
        ("criado", "convite", None),
        ("Convite.Criado", "convite", None),
        ("convite.criado", "Convite", None),
        ("convite.criado", "convite", "a|b"),
    ],
)
def test_formato_do_evento(acao: str, entidade: str, entidade_id: str | None) -> None:
    with pytest.raises(EventoInvalido):
        preparar_evento(acao=acao, entidade=entidade, entidade_id=entidade_id, dados=None)


def test_dados_em_json_com_chaves_ordenadas() -> None:
    _, dados = preparar_evento(
        acao="membro.alterado",
        entidade="membro",
        entidade_id=uuid4(),
        dados={"b": 1, "a": [1, "x"]},
    )
    assert dados == '{"a": [1, "x"], "b": 1}'
