"""Direitos do titular (LGPD): acesso aos proprios dados, pedido de exclusao e anonimizacao.

A pessoa (usuario) existe acima dos clientes; tudo passa pelas funcoes identidade_lgpd_* e
identidade_* (app_identidade), que so devolvem dados da propria pessoa. A anonimizacao e feita
pela plataforma, pela linha de comando, e e irreversivel: nome e e-mail viram um codigo, os
vinculos de login somem, acessos, convites e concessoes se encerram. Trilha e dados aprovados
ficam com o id, que ja nao identifica ninguem (nada some).

Dados que o cliente guarda como controlador (por exemplo, nomes dentro de evidencias) nao sao
tocados aqui: o pedido sobre eles e com o cliente.
"""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.tenancy import ContextoTenant, Escopo, aplicar_contexto


class PessoaJaAnonimizada(ValueError):
    """A anonimizacao ja foi feita (ela so acontece uma vez)."""


def _texto(valor: Any) -> Any:
    if isinstance(valor, datetime):
        return valor.isoformat()
    if isinstance(valor, UUID):
        return str(valor)
    return valor


async def meus_dados(sessao: AsyncSession, usuario_id: UUID) -> dict[str, Any]:
    """Tudo o que a plataforma guarda sobre a pessoa, para baixar (acesso e portabilidade)."""
    usuario = (
        await sessao.execute(
            text("SELECT email, nome FROM identidade_usuario(:u)"), {"u": usuario_id}
        )
    ).first()
    acessos = await sessao.execute(
        text("SELECT tenant_id, tenant_nome, perfil FROM identidade_acessos(:u)"),
        {"u": usuario_id},
    )
    concessoes = await sessao.execute(
        text("SELECT tenant_nome, empresa_nome, ano, expira_em FROM identidade_concessoes(:u)"),
        {"u": usuario_id},
    )
    pedidos = await sessao.execute(
        text(
            "SELECT pedido_id, tipo, solicitado_em, executado_em FROM identidade_lgpd_pedidos(:u)"
        ),
        {"u": usuario_id},
    )
    return {
        "pessoa": {
            "id": str(usuario_id),
            "nome": None if usuario is None else usuario.nome,
            "email": None if usuario is None else usuario.email,
        },
        "acessos": [{k: _texto(v) for k, v in linha._asdict().items()} for linha in acessos],
        "verificacoes": [
            {k: _texto(v) for k, v in linha._asdict().items()} for linha in concessoes
        ],
        "pedidos_lgpd": [{k: _texto(v) for k, v in linha._asdict().items()} for linha in pedidos],
        "observacao": (
            "A trilha de auditoria guarda só o seu id, sem nome ou e-mail. Dados lançados em nome"
            " de um cliente (inventário, evidências) são do cliente: peça a ele."
        ),
    }


def como_json(dados: dict[str, Any]) -> bytes:
    return json.dumps(dados, ensure_ascii=False, indent=2).encode("utf-8")


async def pedidos(sessao: AsyncSession, usuario_id: UUID) -> list[Any]:
    return list(
        await sessao.execute(
            text(
                "SELECT pedido_id, tipo, solicitado_em, executado_em"
                " FROM identidade_lgpd_pedidos(:u)"
            ),
            {"u": usuario_id},
        )
    )


async def pedir_exclusao(sessao: AsyncSession, usuario_id: UUID) -> UUID:
    pedido = await sessao.scalar(text("SELECT identidade_lgpd_pedir(:u)"), {"u": usuario_id})
    return UUID(str(pedido))


async def pendentes(conexao: Any) -> list[Any]:
    return list(
        await conexao.fetch(
            "SELECT pedido_id, usuario_id, solicitado_em FROM identidade_lgpd_pendentes()"
        )
    )


async def anonimizar(conexao: Any, usuario_id: UUID, executor: str) -> list[UUID]:
    """Executa a anonimizacao (uma vez) e registra na trilha de cada cliente da pessoa.

    Tudo numa transacao so: se o registro na trilha de algum cliente falhar, a anonimizacao
    tambem volta e pode ser refeita. Em transacoes separadas, uma falha no meio deixava a pessoa
    anonimizada sem o evento na trilha, e a nova tentativa era recusada (ja anonimizada).
    """
    async with conexao.transaction():
        try:
            tenants = [
                linha[0]
                for linha in await conexao.fetch(
                    "SELECT identidade_lgpd_anonimizar($1, $2)", usuario_id, executor
                )
            ]
        except asyncpg.PostgresError as erro:
            if getattr(erro, "sqlstate", None) == "CL031":
                raise PessoaJaAnonimizada(str(usuario_id)) from erro
            raise
        for tenant_id in tenants:
            # O contexto local vale ate o fim da transacao; cada cliente o troca pelo seu.
            await aplicar_contexto(
                conexao, ContextoTenant(tenant_id=tenant_id, escopo=Escopo.TENANT)
            )
            await audit.registrar_asyncpg(
                conexao,
                tenant_id=tenant_id,
                acao="lgpd.titular_anonimizado",
                entidade="usuario",
                entidade_id=usuario_id,
                dados={"executor": "plataforma"},
            )
    return tenants
