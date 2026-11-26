"""Papeis do banco num Postgres gerenciado (RDS), onde nao ha initdb.

    python -m app.core.papeis_banco

Roda infra/sql/papeis.sql com a conta administrativa e define as senhas dos papeis com login.
Idempotente: rodar de novo so reafirma atributos e senhas. Tudo vem do ambiente (a tarefa
avulsa "papeis" do ECS recebe os segredos do Secrets Manager), nada na linha de comando:

    BANCO_HOST, BANCO_PORTA (5432), BANCO_NOME
    BANCO_ADMIN_USUARIO, BANCO_ADMIN_SENHA
    APP_OWNER_SENHA, APP_USER_SENHA, CURADORIA_SENHA

A conexao exige TLS. Ver docs/runbooks/infra-aws.md.
"""

import asyncio
import os
import sys
from pathlib import Path

import asyncpg

PAPEIS_SQL = Path(__file__).resolve().parents[2] / "infra" / "sql" / "papeis.sql"
PAPEIS_COM_LOGIN = (
    ("app_owner", "APP_OWNER_SENHA"),
    ("app_user", "APP_USER_SENHA"),
    ("app_curadoria", "CURADORIA_SENHA"),
)
SENHA_MINIMA = 20


async def aplicar_papeis(conexao: asyncpg.Connection, senhas: dict[str, str]) -> None:
    """Cria ou reafirma os papeis e define as senhas, numa transacao so."""
    async with conexao.transaction():
        await conexao.execute(PAPEIS_SQL.read_text(encoding="utf-8"))
        for papel, senha in senhas.items():
            # format com %L cita a senha no proprio banco; ela nunca entra num log.
            comando = await conexao.fetchval(
                "SELECT format('ALTER ROLE %I PASSWORD %L', $1::text, $2::text)", papel, senha
            )
            await conexao.execute(comando)


def _exigir(nome: str) -> str:
    valor = os.environ.get(nome, "")
    if not valor:
        raise SystemExit(f"defina {nome}")
    return valor


async def _principal() -> int:
    senhas = {papel: _exigir(variavel) for papel, variavel in PAPEIS_COM_LOGIN}
    for papel, senha in senhas.items():
        if len(senha) < SENHA_MINIMA:
            raise SystemExit(f"senha de {papel} curta demais (minimo {SENHA_MINIMA})")
    conexao = await asyncpg.connect(
        host=_exigir("BANCO_HOST"),
        port=int(os.environ.get("BANCO_PORTA", "5432")),
        database=_exigir("BANCO_NOME"),
        user=_exigir("BANCO_ADMIN_USUARIO"),
        password=_exigir("BANCO_ADMIN_SENHA"),
        # "require", como a DATABASE_URL do app: TLS obrigatorio (o RDS forca rds.force_ssl).
        ssl="require",
        timeout=15,
    )
    try:
        await aplicar_papeis(conexao, senhas)
        linhas = await conexao.fetch(
            "SELECT rolname, rolcanlogin, rolbypassrls FROM pg_roles"
            " WHERE rolname LIKE 'app!_%' ESCAPE '!' ORDER BY rolname"
        )
    finally:
        await conexao.close()
    for linha in linhas:
        print(f"{linha['rolname']}: login={linha['rolcanlogin']} bypassrls={linha['rolbypassrls']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_principal()))
