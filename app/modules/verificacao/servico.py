"""Concessoes de verificacao: conceder, listar e revogar (sessao do administrador).

O verificador recebe por e-mail um link de uso unico (mesmo formato do token de convite:
tenant, concessao e um segredo de 32 bytes, do qual o banco guarda so o SHA-256). Aceito o
link pelo OIDC, ele le um inventario fechado, ate o prazo, e nada mais. O limite mora no banco
(migracao 0010); aqui so ficam as regras de negocio e as mensagens.
"""

import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.erros import AcessoNegado, ColetorDeErros, ErroValidacao
from app.core.security.acesso import Acesso
from app.core.security.convite import TokenConvite, novo_token
from app.modules.organizacao.acessos import normalizar_email

PRAZO_PADRAO_DIAS = 90
PRAZO_MAXIMO_DIAS = 365


@dataclass(frozen=True)
class Concessao:
    concessao_id: UUID
    inventario_id: UUID
    ano: int
    email: str
    criado_em: datetime
    expira_em: datetime
    aceito_em: datetime | None
    revogado_em: datetime | None
    agora: datetime

    @property
    def situacao(self) -> str:
        if self.revogado_em is not None:
            return "revogada"
        if self.expira_em <= self.agora:
            return "vencida"
        return "ativa" if self.aceito_em is not None else "aguardando aceite"


def validar_prazo(texto: str) -> tuple[int | None, str | None]:
    limpo = (texto or "").strip()
    if not limpo:
        return PRAZO_PADRAO_DIAS, None
    if not limpo.isdigit() or not 1 <= int(limpo) <= PRAZO_MAXIMO_DIAS:
        return None, f"Use de 1 a {PRAZO_MAXIMO_DIAS} dias. O padrão é {PRAZO_PADRAO_DIAS}."
    return int(limpo), None


async def conceder(
    sessao: AsyncSession, acesso: Acesso, formulario: dict[str, str]
) -> tuple[TokenConvite, str, int, int]:
    """Grava a concessao. Devolve (token, e-mail, ano do inventario, prazo em dias)."""
    coletor = ColetorDeErros()
    try:
        email = normalizar_email(formulario.get("email", ""))
    except ErroValidacao as recusado:
        coletor.adicionar("email", recusado.mensagem)
        email = ""
    prazo, erro_prazo = validar_prazo(formulario.get("prazo_dias", ""))
    if erro_prazo:
        coletor.adicionar("prazo_dias", erro_prazo)
    inventario = None
    with contextlib.suppress(ValueError):
        inventario = (
            await sessao.execute(
                text(
                    "SELECT inventario_id, empresa_id, ano, estado FROM inventario"
                    " WHERE inventario_id = :id"
                ),
                {"id": UUID(formulario.get("inventario_id", ""))},
            )
        ).first()
    if inventario is None:
        coletor.adicionar("inventario_id", "Escolha o inventário a verificar.")
    elif inventario.estado != "fechado":
        coletor.adicionar(
            "inventario_id", "Só um inventário fechado vai para verificação. Feche-o antes."
        )
    coletor.levantar_se_houver()
    if inventario is None or prazo is None:  # o coletor ja recusou; aqui so para o tipo
        raise AcessoNegado

    token = novo_token(acesso.tenant_id, uuid4())
    await sessao.execute(
        text(
            "INSERT INTO concessao_verificacao (tenant_id, concessao_id, empresa_id,"
            " inventario_id, email, segredo_hash, criado_por, expira_em)"
            " VALUES (:tenant, :id, :empresa, :inventario, :email, :hash, :usuario,"
            " now() + :prazo)"
        ),
        {
            "tenant": acesso.tenant_id,
            "id": token.convite_id,
            "empresa": inventario.empresa_id,
            "inventario": inventario.inventario_id,
            "email": email,
            "hash": token.segredo_hash,
            "usuario": acesso.usuario_id,
            "prazo": timedelta(days=prazo),
        },
    )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="concessao.criada",
        entidade="concessao_verificacao",
        entidade_id=token.convite_id,
        dados={
            "empresa_id": str(inventario.empresa_id),
            "inventario_id": str(inventario.inventario_id),
            "prazo_dias": prazo,
        },
    )
    return token, email, inventario.ano, prazo


async def listar(sessao: AsyncSession, empresa_id: UUID) -> list[Concessao]:
    linhas = await sessao.execute(
        text(
            "SELECT c.concessao_id, c.inventario_id, i.ano, c.email, c.criado_em, c.expira_em,"
            " c.aceito_em, c.revogado_em, now() AS agora FROM concessao_verificacao c"
            " JOIN inventario i ON i.tenant_id = c.tenant_id AND i.inventario_id = c.inventario_id"
            " WHERE c.empresa_id = :empresa ORDER BY c.criado_em DESC"
        ),
        {"empresa": empresa_id},
    )
    return [Concessao(**linha._asdict()) for linha in linhas]


async def revogar(sessao: AsyncSession, acesso: Acesso, concessao_id: UUID) -> None:
    resultado = await sessao.execute(
        text(
            "UPDATE concessao_verificacao SET revogado_em = now(), revogado_por = :usuario"
            " WHERE concessao_id = :id AND revogado_em IS NULL RETURNING empresa_id"
        ),
        {"id": concessao_id, "usuario": acesso.usuario_id},
    )
    empresa_id = resultado.scalar()
    if empresa_id is None:
        raise AcessoNegado
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="concessao.revogada",
        entidade="concessao_verificacao",
        entidade_id=concessao_id,
        dados={"empresa_id": str(empresa_id)},
    )


def texto_da_concessao(
    nome_tenant: str, nome_empresa: str, ano: int, links: dict[str, str], prazo: int
) -> tuple[str, str]:
    rotulos = {"google": "Google", "microsoft": "Microsoft", "dev": "provedor de desenvolvimento"}
    linhas = [f"Entrar com {rotulos.get(nome, nome)}: {url}" for nome, url in links.items()]
    assunto = f"Verificação do inventário {ano} de {nome_empresa}"
    corpo = (
        f"{nome_tenant} concedeu a você acesso de leitura ao inventário de emissões {ano} de"
        f" {nome_empresa} no CarbonLedger, para verificação.\n\n"
        "Para aceitar, entre com a conta deste mesmo e-mail:\n\n"
        + "\n".join(linhas)
        + f"\n\nO link só pode ser usado uma vez. O acesso vale por {prazo} dias e pode ser"
        " revogado pelo cliente a qualquer momento.\n"
        "Se você não esperava esta mensagem, ignore-a.\n"
    )
    return assunto, corpo
