"""Trilha de auditoria: registro (na mesma transacao da acao) e verificacao da cadeia.

O banco calcula seq e hash (trigger audit_encadear). Este modulo so manda o evento e, na
verificacao, refaz a cadeia com a definicao v1 escrita na migracao 0003, sem confiar na
funcao do banco: quem verifica nao depende de quem gravou.

A trilha nunca e apagada; por isso nao leva dado pessoal (e-mail, nome, documento). Ela
guarda identificadores (usuario_id, convite_id...), e a LGPD trata a tabela usuario.
"""

import hashlib
import json
import re
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

HASH_INICIAL = bytes(32)
VERSAO_CONTEUDO = "v1"

_ACAO = re.compile(r"^[a-z][a-z_]*\.[a-z][a-z_]*$")
_ENTIDADE = re.compile(r"^[a-z][a-z_]*$")

# Chaves que nunca entram em dados: a trilha e permanente e nao pode guardar dado pessoal.
CHAVES_PROIBIDAS = frozenset(
    {"email", "nome", "cpf", "rg", "telefone", "celular", "endereco", "nascimento", "senha"}
)


class EventoInvalido(ValueError):
    """Evento de auditoria mal formado ou com dado pessoal."""


def _validar_dados(dados: Any, caminho: str = "dados") -> None:
    if isinstance(dados, Mapping):
        for chave, valor in dados.items():
            if not isinstance(chave, str):
                raise EventoInvalido(f"{caminho}: chave nao textual")
            if chave.lower() in CHAVES_PROIBIDAS:
                raise EventoInvalido(f"{caminho}.{chave}: dado pessoal nao entra na trilha")
            _validar_dados(valor, f"{caminho}.{chave}")
    elif isinstance(dados, list | tuple):
        for indice, valor in enumerate(dados):
            _validar_dados(valor, f"{caminho}[{indice}]")
    elif not (dados is None or isinstance(dados, str | int | float | bool)):
        raise EventoInvalido(f"{caminho}: tipo {type(dados).__name__} nao suportado")


def preparar_evento(
    *,
    acao: str,
    entidade: str,
    entidade_id: str | UUID | None,
    dados: Mapping[str, Any] | None,
) -> tuple[str | None, str]:
    """Valida e devolve (entidade_id em texto, dados em JSON)."""
    if not _ACAO.fullmatch(acao):
        raise EventoInvalido(f"acao invalida: {acao!r} (use modulo.verbo)")
    if not _ENTIDADE.fullmatch(entidade):
        raise EventoInvalido(f"entidade invalida: {entidade!r}")
    entidade_texto = None if entidade_id is None else str(entidade_id)
    if entidade_texto is not None and "|" in entidade_texto:
        raise EventoInvalido("entidade_id nao pode conter |")
    conteudo = dict(dados or {})
    _validar_dados(conteudo)
    return entidade_texto, json.dumps(conteudo, ensure_ascii=False, sort_keys=True)


_SQL_INSERIR = (
    "INSERT INTO audit_log (tenant_id, seq, evento_id, ocorrido_em, usuario_id, acao, entidade,"
    " entidade_id, dados, hash_anterior, hash)"
    " VALUES ({tenant}, 0, gen_random_uuid(), now(), {usuario}, {acao}, {entidade},"
    " {entidade_id}, CAST({dados} AS jsonb), '\\x', '\\x')"
)


async def registrar(
    sessao: AsyncSession,
    *,
    tenant_id: UUID,
    acao: str,
    entidade: str,
    entidade_id: str | UUID | None = None,
    usuario_id: UUID | None = None,
    dados: Mapping[str, Any] | None = None,
) -> None:
    """Grava o evento na transacao da sessao (o RLS confere o tenant)."""
    entidade_texto, dados_json = preparar_evento(
        acao=acao, entidade=entidade, entidade_id=entidade_id, dados=dados
    )
    await sessao.execute(
        text(
            _SQL_INSERIR.format(
                tenant=":tenant",
                usuario=":usuario",
                acao=":acao",
                entidade=":entidade",
                entidade_id=":entidade_id",
                dados=":dados",
            )
        ),
        {
            "tenant": tenant_id,
            "usuario": usuario_id,
            "acao": acao,
            "entidade": entidade,
            "entidade_id": entidade_texto,
            "dados": dados_json,
        },
    )


async def registrar_asyncpg(
    conexao: Any,
    *,
    tenant_id: UUID,
    acao: str,
    entidade: str,
    entidade_id: str | UUID | None = None,
    usuario_id: UUID | None = None,
    dados: Mapping[str, Any] | None = None,
) -> None:
    """Mesma coisa para conexao asyncpg (workers e linha de comando)."""
    entidade_texto, dados_json = preparar_evento(
        acao=acao, entidade=entidade, entidade_id=entidade_id, dados=dados
    )
    await conexao.execute(
        _SQL_INSERIR.format(
            tenant="$1", usuario="$2", acao="$3", entidade="$4", entidade_id="$5", dados="$6"
        ),
        tenant_id,
        usuario_id,
        acao,
        entidade,
        entidade_texto,
        dados_json,
    )


# ---------------------------------------------------------------- verificacao


@dataclass(frozen=True)
class LinhaTrilha:
    tenant_id: UUID
    seq: int
    evento_id: UUID
    ocorrido_em: datetime
    usuario_id: UUID | None
    acao: str
    entidade: str
    entidade_id: str | None
    dados_texto: str
    hash_anterior: bytes
    hash: bytes


@dataclass(frozen=True)
class ResultadoVerificacao:
    integra: bool
    eventos: int
    quebra_em: int | None = None
    motivo: str | None = None


def conteudo_v1(linha: LinhaTrilha) -> bytes:
    instante = linha.ocorrido_em.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    partes = [
        VERSAO_CONTEUDO,
        str(linha.tenant_id),
        str(linha.seq),
        str(linha.evento_id),
        instante,
        "" if linha.usuario_id is None else str(linha.usuario_id),
        linha.acao,
        linha.entidade,
        linha.entidade_id or "",
        linha.dados_texto,
    ]
    return "|".join(partes).encode("utf-8")


def hash_da_linha(linha: LinhaTrilha) -> bytes:
    return hashlib.sha256(linha.hash_anterior + conteudo_v1(linha)).digest()


def verificar_cadeia(linhas: Iterable[LinhaTrilha]) -> ResultadoVerificacao:
    """Confere sequencia, encadeamento e hash de cada evento, em ordem de seq."""
    esperado_seq = 1
    hash_anterior = HASH_INICIAL
    total = 0
    for linha in linhas:
        total += 1
        if linha.seq != esperado_seq:
            return ResultadoVerificacao(False, total, linha.seq, "sequencia com falha")
        if linha.hash_anterior != hash_anterior:
            return ResultadoVerificacao(False, total, linha.seq, "encadeamento rompido")
        if hash_da_linha(linha) != linha.hash:
            return ResultadoVerificacao(False, total, linha.seq, "conteudo alterado")
        hash_anterior = linha.hash
        esperado_seq += 1
    return ResultadoVerificacao(True, total)


_SQL_LER = text(
    "SELECT tenant_id, seq, evento_id, ocorrido_em, usuario_id, acao, entidade, entidade_id,"
    " dados::text AS dados_texto, hash_anterior, hash"
    " FROM audit_log WHERE tenant_id = :tenant ORDER BY seq"
)


async def _ler(sessao: AsyncSession, tenant_id: UUID) -> AsyncIterator[LinhaTrilha]:
    resultado = await sessao.stream(_SQL_LER, {"tenant": tenant_id})
    async for linha in resultado:
        yield LinhaTrilha(**linha._asdict())


async def verificar_tenant(sessao: AsyncSession, tenant_id: UUID) -> ResultadoVerificacao:
    linhas = [linha async for linha in _ler(sessao, tenant_id)]
    return verificar_cadeia(linhas)
