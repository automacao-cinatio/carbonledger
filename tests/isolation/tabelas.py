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
    "grupo": False,
    "unidade": True,
    "inventario": True,
    "fonte_emissao": True,
    "planilha_importacao": True,
    "lancamento": True,
    "lancamento_decisao": True,
    "evidencia": True,
    "calculo": True,
    "calculo_linha": True,
    "calculo_pendencia": True,
    "inventario_evento": True,
    "concessao_verificacao": True,
    "meta": True,
    "acao_reducao": True,
    "integridade_verificacao": False,
    "campanha": True,
    "campanha_envio": True,
    "certificado_assinatura": True,
    "relatorio_inventario": True,
}

# O que o verificador ve com uma concessao valida (migracao 0010). Fora daqui, nada.
VISIVEIS_AO_VERIFICADOR = frozenset(
    {
        "tenant",
        "empresa",
        "grupo",
        "unidade",
        "inventario",
        "fonte_emissao",
        "planilha_importacao",
        "calculo",
        "inventario_evento",
        "lancamento",
        "evidencia",
        "lancamento_decisao",
        "calculo_linha",
        "calculo_pendencia",
        "concessao_verificacao",
        # Migracao 0017: o relatorio assinado do inventario e o certificado que o assinou.
        "relatorio_inventario",
        "certificado_assinatura",
    }
)

# Tabelas em que o verificador so le algumas colunas (privilegio por coluna).
COLUNAS_DO_VERIFICADOR: dict[str, str] = {
    "certificado_assinatura": "tenant_id, certificado_id, empresa_id, titular, cnpj, emissor,"
    " numero_serie, valido_de, valido_ate, impressao_sha256, enviado_por, enviado_em,"
    " desativado_em, desativado_por",
}

# Dado de cliente so de insercao (trigger somente_insercao): o app_user so le e insere.
TABELAS_SO_INSERCAO = (
    "planilha_importacao",
    "lancamento",
    "lancamento_decisao",
    "evidencia",
    "calculo",
    "calculo_linha",
    "calculo_pendencia",
    "inventario_evento",
    "integridade_verificacao",
    "campanha_envio",
    "relatorio_inventario",
)

# Tabelas sem tenant_id, de proposito. Cada entrada precisa de justificativa.
TABELAS_GLOBAIS: dict[str, str] = {
    "alembic_version": "controle de versao das migracoes",
    "usuario": "a pessoa existe antes de escolher um tenant; RLS proprio (si mesma e membros)",
    "identidade": "vinculo com o provedor OIDC; so as funcoes identidade_* leem",
    "lote_importacao": "referencia global de fatores (nao e dado de cliente); so insercao",
    "lote_decisao": "aprovacao da referencia global; so insercao",
    "gwp_versao": "GWP de referencia, global; so insercao",
    "fator_versao": "fatores de emissao de referencia, globais; so insercao",
    "mistura_versao": "misturas de combustivel de referencia, globais; so insercao",
    "lgpd_pedido": "pedido do titular, acima dos clientes; so as funcoes identidade_lgpd_* leem",
}

# Referencia global: o app_user so le; so o app_curadoria insere; ninguem altera.
TABELAS_DE_REFERENCIA = (
    "lote_importacao",
    "lote_decisao",
    "gwp_versao",
    "fator_versao",
    "mistura_versao",
)

# Globais que, mesmo sem tenant_id, tem RLS ligado com FORCE.
GLOBAIS_COM_RLS = frozenset({"usuario", "identidade", "lgpd_pedido"})

# Unicas funcoes do app_identidade (BYPASSRLS). Funcao nova aqui exige revisao de seguranca.
FUNCOES_IDENTIDADE = frozenset(
    {
        "identidade_localizar",
        "identidade_usuario",
        "identidade_acessos",
        "identidade_acesso",
        "identidade_aceitar_convite",
        # P12, revisadas na migracao 0010: aceite da concessao e concessoes ativas da pessoa.
        "identidade_aceitar_concessao",
        "identidade_concessoes",
        # P20, revisada na migracao 0013: so os ids dos tenants, para o disparo diario.
        "identidade_tenants",
        # P23, revisadas na migracao 0015: pedidos do titular e anonimizacao.
        "identidade_lgpd_pedir",
        "identidade_lgpd_pedidos",
        "identidade_lgpd_pendentes",
        "identidade_lgpd_anonimizar",
    }
)
