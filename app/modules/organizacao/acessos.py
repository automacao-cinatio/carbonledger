"""Convites e membros do tenant. Toda consulta passa pelo RLS da sessao de tenant.

Regras de perfil: administrador e gestor veem o tenant inteiro e nao levam empresas;
responsavel e colaborador precisam de pelo menos uma empresa do tenant.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.erros import AcessoNegado, ErroValidacao
from app.core.security.acesso import Acesso, Perfil
from app.core.security.convite import TokenConvite, novo_token

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TAMANHO_MAXIMO_EMAIL = 254


def normalizar_email(email: str) -> str:
    limpo = email.strip().lower()
    if len(limpo) > TAMANHO_MAXIMO_EMAIL or not _EMAIL.fullmatch(limpo):
        raise ErroValidacao("email", "Informe um e-mail completo, como nome@empresa.com.br.")
    return limpo


async def validar_perfil_e_empresas(
    sessao: AsyncSession, perfil: Perfil, empresas: list[UUID]
) -> list[UUID]:
    unicas = sorted(set(empresas))
    if perfil.ve_o_tenant_inteiro:
        if unicas:
            raise ErroValidacao(
                "empresas",
                "Administrador e gestor já veem todas as empresas: deixe a lista vazia.",
            )
        return []
    if not unicas:
        raise ErroValidacao(
            "empresas", "Escolha pelo menos uma empresa para o responsável ou colaborador."
        )
    # O RLS so deixa contar empresas do proprio tenant: empresa alheia some da contagem.
    encontradas = await sessao.scalar(
        text("SELECT count(*) FROM empresa WHERE empresa_id = ANY(:empresas)"),
        {"empresas": unicas},
    )
    if encontradas != len(unicas):
        raise ErroValidacao(
            "empresas", "Uma das empresas não foi encontrada. Atualize a página e escolha de novo."
        )
    return unicas


# ---------------------------------------------------------------- convites


@dataclass(frozen=True)
class Convite:
    convite_id: UUID
    email: str
    perfil: Perfil
    empresas: list[UUID]
    criado_em: datetime
    expira_em: datetime
    situacao: str


async def criar_convite(
    sessao: AsyncSession,
    *,
    tenant_id: UUID,
    criado_por: UUID | None,
    email: str,
    perfil: Perfil,
    empresas: list[UUID],
    validade: timedelta,
) -> TokenConvite:
    email_limpo = normalizar_email(email)
    empresas_validas = await validar_perfil_e_empresas(sessao, perfil, empresas)
    token = novo_token(tenant_id, uuid4())
    await sessao.execute(
        text(
            "INSERT INTO convite (tenant_id, convite_id, email, perfil, empresas, segredo_hash,"
            " criado_por, expira_em)"
            " VALUES (:tenant, :convite, :email, :perfil, :empresas, :hash, :criado_por,"
            " now() + :validade)"
        ),
        {
            "tenant": tenant_id,
            "convite": token.convite_id,
            "email": email_limpo,
            "perfil": perfil.value,
            "empresas": empresas_validas,
            "hash": token.segredo_hash,
            "criado_por": criado_por,
            "validade": validade,
        },
    )
    await audit.registrar(
        sessao,
        tenant_id=tenant_id,
        usuario_id=criado_por,
        acao="convite.criado",
        entidade="convite",
        entidade_id=token.convite_id,
        dados={"perfil": perfil.value, "empresas": [str(e) for e in empresas_validas]},
    )
    return token


async def listar_convites(sessao: AsyncSession) -> list[Convite]:
    linhas = await sessao.execute(
        text(
            "SELECT convite_id, email, perfil, empresas, criado_em, expira_em,"
            " CASE WHEN aceito_em IS NOT NULL THEN 'aceito'"
            "      WHEN revogado_em IS NOT NULL THEN 'revogado'"
            "      WHEN expira_em <= now() THEN 'expirado'"
            "      ELSE 'pendente' END AS situacao"
            " FROM convite ORDER BY criado_em DESC"
        )
    )
    return [
        Convite(
            convite_id=linha.convite_id,
            email=linha.email,
            perfil=Perfil(linha.perfil),
            empresas=list(linha.empresas),
            criado_em=linha.criado_em,
            expira_em=linha.expira_em,
            situacao=linha.situacao,
        )
        for linha in linhas
    ]


async def revogar_convite(sessao: AsyncSession, acesso: Acesso, convite_id: UUID) -> None:
    resultado = await sessao.execute(
        text(
            "UPDATE convite SET revogado_em = now()"
            " WHERE convite_id = :convite AND aceito_em IS NULL AND revogado_em IS NULL"
        ),
        {"convite": convite_id},
    )
    if resultado.rowcount != 1:  # type: ignore[attr-defined]
        raise AcessoNegado
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="convite.revogado",
        entidade="convite",
        entidade_id=convite_id,
    )


def texto_do_convite(
    nome_tenant: str, links: dict[str, str], validade: timedelta
) -> tuple[str, str]:
    rotulos = {"google": "Google", "microsoft": "Microsoft", "dev": "provedor de desenvolvimento"}
    linhas = [f"Entrar com {rotulos.get(nome, nome)}: {url}" for nome, url in links.items()]
    assunto = f"Convite para o CarbonLedger: {nome_tenant}"
    corpo = (
        f"Você foi convidado para acessar o inventário de emissões de {nome_tenant} no"
        " CarbonLedger.\n\n"
        "Para aceitar, entre com a conta corporativa deste mesmo e-mail:\n\n"
        + "\n".join(linhas)
        + f"\n\nO convite vale por {validade.days} dias e só pode ser usado uma vez.\n"
        "Se você não esperava este convite, ignore esta mensagem.\n"
    )
    return assunto, corpo


# ---------------------------------------------------------------- membros


@dataclass(frozen=True)
class Membro:
    usuario_id: UUID
    nome: str
    email: str
    perfil: Perfil
    ativo: bool
    empresas: list[UUID]


async def listar_membros(sessao: AsyncSession) -> list[Membro]:
    linhas = await sessao.execute(
        text(
            "SELECT m.usuario_id, u.nome, u.email, m.perfil, m.ativo,"
            " coalesce(array_agg(me.empresa_id ORDER BY me.empresa_id)"
            "          FILTER (WHERE me.empresa_id IS NOT NULL), '{}') AS empresas"
            " FROM membro m"
            " JOIN usuario u ON u.usuario_id = m.usuario_id"
            " LEFT JOIN membro_empresa me"
            "   ON me.tenant_id = m.tenant_id AND me.usuario_id = m.usuario_id"
            " GROUP BY m.usuario_id, u.nome, u.email, m.perfil, m.ativo"
            " ORDER BY u.nome, m.usuario_id"
        )
    )
    return [
        Membro(
            usuario_id=linha.usuario_id,
            nome=linha.nome,
            email=linha.email,
            perfil=Perfil(linha.perfil),
            ativo=linha.ativo,
            empresas=list(linha.empresas),
        )
        for linha in linhas
    ]


def _nao_pode_ser_voce(acesso: Acesso, usuario_id: UUID) -> None:
    if usuario_id == acesso.usuario_id:
        raise ErroValidacao(
            "usuario_id",
            "Você não pode mudar o próprio acesso. Peça a outro administrador.",
        )


async def alterar_membro(
    sessao: AsyncSession,
    acesso: Acesso,
    usuario_id: UUID,
    *,
    perfil: Perfil,
    empresas: list[UUID],
) -> None:
    _nao_pode_ser_voce(acesso, usuario_id)
    empresas_validas = await validar_perfil_e_empresas(sessao, perfil, empresas)
    resultado = await sessao.execute(
        text(
            "UPDATE membro SET perfil = :perfil, atualizado_em = now() WHERE usuario_id = :usuario"
        ),
        {"perfil": perfil.value, "usuario": usuario_id},
    )
    if resultado.rowcount != 1:  # type: ignore[attr-defined]
        raise AcessoNegado
    await sessao.execute(
        text("DELETE FROM membro_empresa WHERE usuario_id = :usuario"), {"usuario": usuario_id}
    )
    if empresas_validas:
        await sessao.execute(
            text(
                "INSERT INTO membro_empresa (tenant_id, usuario_id, empresa_id)"
                " SELECT :tenant, :usuario, unnest(CAST(:empresas AS uuid[]))"
            ),
            {"tenant": acesso.tenant_id, "usuario": usuario_id, "empresas": empresas_validas},
        )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="membro.alterado",
        entidade="membro",
        entidade_id=usuario_id,
        dados={"perfil": perfil.value, "empresas": [str(e) for e in empresas_validas]},
    )


async def definir_ativo(
    sessao: AsyncSession, acesso: Acesso, usuario_id: UUID, *, ativo: bool
) -> None:
    _nao_pode_ser_voce(acesso, usuario_id)
    resultado = await sessao.execute(
        text("UPDATE membro SET ativo = :ativo, atualizado_em = now() WHERE usuario_id = :usuario"),
        {"ativo": ativo, "usuario": usuario_id},
    )
    if resultado.rowcount != 1:  # type: ignore[attr-defined]
        raise AcessoNegado
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="membro.reativado" if ativo else "membro.desativado",
        entidade="membro",
        entidade_id=usuario_id,
    )
