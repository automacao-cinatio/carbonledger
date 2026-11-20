"""Registro das tabelas cobertas pela suite de vazamento.

Toda tabela com tenant_id entra aqui no mesmo PR que a cria; o teste de cobertura falha
se o banco tiver tabela com tenant_id fora deste registro (ou o contrario).
Valor: True quando a tabela tem empresa_id e, portanto, politica restritiva de empresa.
"""

TABELAS_COM_TENANT: dict[str, bool] = {
    "tenant": False,
    "empresa": True,
}

# Tabelas sem tenant_id, de proposito. Cada entrada precisa de justificativa.
TABELAS_GLOBAIS: dict[str, str] = {
    "alembic_version": "controle de versao das migracoes",
}
