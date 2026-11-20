"""Registro das tabelas cobertas pela suite de vazamento.

Toda tabela com tenant_id entra aqui no mesmo PR que a cria; o teste de cobertura falha
se o banco tiver tabela com tenant_id fora deste registro (ou o contrario).
Valor: True quando a tabela tem empresa_id e, portanto, politica restritiva de empresa.
"""

TABELAS_COM_TENANT: dict[str, bool] = {
    "tenant": False,
    "empresa": True,
    "membro": False,
    "membro_empresa": True,
    "convite": False,
    "audit_log": False,
}

# Tabelas sem tenant_id, de proposito. Cada entrada precisa de justificativa.
TABELAS_GLOBAIS: dict[str, str] = {
    "alembic_version": "controle de versao das migracoes",
    "usuario": "a pessoa existe antes de escolher um tenant; RLS proprio (si mesma e membros)",
    "identidade": "vinculo com o provedor OIDC; so as funcoes identidade_* leem",
}

# Globais que, mesmo sem tenant_id, tem RLS ligado com FORCE.
GLOBAIS_COM_RLS = frozenset({"usuario", "identidade"})

# Unicas funcoes do app_identidade (BYPASSRLS). Funcao nova aqui exige revisao de seguranca.
FUNCOES_IDENTIDADE = frozenset(
    {
        "identidade_localizar",
        "identidade_usuario",
        "identidade_acessos",
        "identidade_acesso",
        "identidade_aceitar_convite",
    }
)
