"""Papeis no banco gerenciado: o comando e idempotente e as senhas valem."""

import asyncpg
import pytest

from app.core.papeis_banco import aplicar_papeis
from tests.isolation.conftest import SENHA_CURADORIA, SENHA_OWNER, SENHA_USER, Banco


async def test_aplicar_de_novo_nao_muda_nada_e_senhas_seguem_valendo(banco: Banco) -> None:
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        await aplicar_papeis(
            conexao,
            {"app_owner": SENHA_OWNER, "app_user": SENHA_USER, "app_curadoria": SENHA_CURADORIA},
        )
        papeis = {
            linha["rolname"]: (linha["rolcanlogin"], linha["rolbypassrls"])
            for linha in await conexao.fetch(
                "SELECT rolname, rolcanlogin, rolbypassrls FROM pg_roles"
                " WHERE rolname LIKE 'app!_%' ESCAPE '!'"
            )
        }
    finally:
        await conexao.close()
    assert papeis == {
        "app_owner": (True, False),
        "app_user": (True, False),
        "app_verificador": (False, False),
        "app_identidade": (False, True),
        "app_curadoria": (True, False),
    }
    usuario = await asyncpg.connect(banco.dsn_app)
    await usuario.close()


async def test_falha_no_meio_desfaz_tudo(banco: Banco) -> None:
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        with pytest.raises(asyncpg.PostgresError):
            await aplicar_papeis(
                conexao, {"app_user": "trocada-nunca", "papel_que_nao_existe": "x"}
            )
    finally:
        await conexao.close()
    # A senha do app_user nao mudou: a troca foi desfeita junto com o erro.
    usuario = await asyncpg.connect(banco.dsn_app)
    await usuario.close()
