"""SQL de RLS e de permissoes usado pelas migracoes.

Toda tabela de negocio passa por sql_rls_tabela(): ENABLE e FORCE, politica permissiva de
tenant e, quando tem empresa_id, politica restritiva de empresa. Sem contexto, nada aparece.

Regra: uma funcao daqui ja usada por migracao nunca muda de comportamento, porque migracoes
antigas a chamam de novo ao recriar o banco. Comportamento novo vira funcao nova.
"""

import re
from collections.abc import Sequence

PAPEL_APP = "app_user"
PAPEL_VERIFICADOR = "app_verificador"

PRIVILEGIOS_VALIDOS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})

_IDENTIFICADOR = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

# Funcoes de contexto (criadas na migracao 0001). Lidas por transacao via set_config(.., true).
FILTRO_TENANT = "tenant_id = (SELECT contexto_tenant_id())"
FILTRO_EMPRESA = (
    "(SELECT contexto_escopo()) = 'tenant'"
    " OR ((SELECT contexto_escopo()) = 'empresas'"
    " AND empresa_id = ANY ((SELECT contexto_empresas())::uuid[]))"
)


def _identificador(nome: str) -> str:
    """Aceita so identificadores simples: o nome entra direto no SQL."""
    if not _IDENTIFICADOR.fullmatch(nome):
        raise ValueError(f"identificador invalido para SQL: {nome!r}")
    return nome


def sql_rls_tabela(tabela: str, *, por_empresa: bool) -> list[str]:
    """Liga RLS com falha fechada: sem app.tenant_id (ou com escopo invalido) nada aparece."""
    t = _identificador(tabela)
    comandos = [
        f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {t}_tenant ON {t} AS PERMISSIVE FOR ALL TO PUBLIC"
        f" USING ({FILTRO_TENANT}) WITH CHECK ({FILTRO_TENANT})",
    ]
    if por_empresa:
        comandos.append(
            f"CREATE POLICY {t}_empresa ON {t} AS RESTRICTIVE FOR ALL TO PUBLIC"
            f" USING ({FILTRO_EMPRESA}) WITH CHECK ({FILTRO_EMPRESA})"
        )
    return comandos


def sql_conceder(tabela: str, privilegios_app: Sequence[str]) -> list[str]:
    """Concede ao app_user so os privilegios pedidos; o verificador recebe so SELECT."""
    t = _identificador(tabela)
    pedidos = [p.upper() for p in privilegios_app]
    invalidos = set(pedidos) - PRIVILEGIOS_VALIDOS
    if invalidos or not pedidos:
        raise ValueError(f"privilegios invalidos: {sorted(invalidos) or 'nenhum'}")
    return [
        f"REVOKE ALL ON {t} FROM PUBLIC",
        f"GRANT {', '.join(pedidos)} ON {t} TO {PAPEL_APP}",
        f"GRANT SELECT ON {t} TO {PAPEL_VERIFICADOR}",
    ]
