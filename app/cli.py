"""Comandos de operacao da CINATIO.

    python -m app.cli criar-tenant --nome "Cliente S.A." --email admin@cliente.com.br

Cria o tenant e um convite de administrador, e mostra os links do convite (um por provedor
habilitado). O operador repassa o link; o convite so vale para quem entrar com esse e-mail.
"""

import argparse
import asyncio
import sys
from datetime import timedelta
from uuid import UUID, uuid4

from app.core.config import Configuracao, obter_configuracao
from app.core.security.convite import novo_token
from app.core.security.oidc import provedores_habilitados
from app.core.tenancy import ContextoTenant, Escopo, conexao_de_tenant
from app.modules.organizacao.acessos import normalizar_email


async def criar_tenant(configuracao: Configuracao, nome: str, email: str) -> tuple[UUID, str]:
    """(tenant_id, token do convite). Roda como app_user, com o contexto do tenant novo."""
    nome_limpo = nome.strip()
    if not nome_limpo:
        raise ValueError("Informe o nome do cliente.")
    email_limpo = normalizar_email(email)
    tenant_id = uuid4()
    token = novo_token(tenant_id, uuid4())
    validade = timedelta(days=configuracao.convite_validade_dias)
    contexto = ContextoTenant(tenant_id=tenant_id, escopo=Escopo.TENANT)
    async with conexao_de_tenant(configuracao.database_url, contexto) as conexao:
        await conexao.execute(
            "INSERT INTO tenant (tenant_id, nome) VALUES ($1, $2)", tenant_id, nome_limpo
        )
        await conexao.execute(
            "INSERT INTO convite (tenant_id, convite_id, email, perfil, segredo_hash, expira_em)"
            " VALUES ($1, $2, $3, 'administrador', $4, now() + $5)",
            tenant_id,
            token.convite_id,
            email_limpo,
            token.segredo_hash,
            validade,
        )
    return tenant_id, token.codificar()


def main(argumentos: list[str] | None = None) -> int:
    analisador = argparse.ArgumentParser(prog="python -m app.cli")
    comandos = analisador.add_subparsers(dest="comando", required=True)
    criar = comandos.add_parser("criar-tenant", help="cria um tenant e o convite do administrador")
    criar.add_argument("--nome", required=True)
    criar.add_argument("--email", required=True)
    args = analisador.parse_args(argumentos)

    configuracao = obter_configuracao()
    tenant_id, token = asyncio.run(criar_tenant(configuracao, args.nome, args.email))
    base = configuracao.app_url_base.rstrip("/")
    print(f"Tenant criado: {tenant_id}")
    print("Links do convite de administrador (repasse so para a pessoa convidada):")
    for provedor in provedores_habilitados(configuracao):
        print(f"  {provedor}: {base}/convite/{token}/{provedor}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
