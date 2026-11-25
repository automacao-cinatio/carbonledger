"""Postgres real com os papeis e as migracoes de verdade, e dados de dois tenants."""

import asyncio
import os
import subprocess  # nosec B404
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient
from testcontainers.community.postgres import PostgresContainer

RAIZ = Path(__file__).resolve().parents[2]
PAPEIS_SQL = RAIZ / "infra" / "sql" / "papeis.sql"

# Senhas so deste conteiner descartavel de teste.
SENHA_OWNER = "owner-teste"
SENHA_USER = "user-teste"
SENHA_CURADORIA = "curadoria-teste"


@dataclass(frozen=True)
class Banco:
    dsn_admin: str
    dsn_owner: str
    dsn_app: str
    dsn_curadoria: str


@dataclass(frozen=True)
class Dados:
    """Dois tenants (A e B), cada um com duas empresas."""

    tenant_a: UUID
    tenant_b: UUID
    empresa_a1: UUID
    empresa_a2: UUID
    empresa_b1: UUID
    empresa_b2: UUID
    grupo_a: UUID
    grupo_b: UUID
    unidade_a1: UUID
    unidade_b1: UUID
    admin_a: UUID
    responsavel_a1: UUID
    admin_b: UUID
    responsavel_b1: UUID
    inventario_a1: UUID
    inventario_b1: UUID
    fonte_a1: UUID
    fonte_b1: UUID
    lancamento_a1: UUID
    lancamento_b1: UUID
    verificador_a: UUID
    verificador_b: UUID
    concessao_a1: UUID
    concessao_b1: UUID


DADOS = Dados(
    tenant_a=UUID("aaaaaaaa-0000-4000-8000-000000000000"),
    tenant_b=UUID("bbbbbbbb-0000-4000-8000-000000000000"),
    empresa_a1=UUID("aaaaaaaa-0000-4000-8000-000000000001"),
    empresa_a2=UUID("aaaaaaaa-0000-4000-8000-000000000002"),
    empresa_b1=UUID("bbbbbbbb-0000-4000-8000-000000000001"),
    empresa_b2=UUID("bbbbbbbb-0000-4000-8000-000000000002"),
    grupo_a=UUID("aaaaaaaa-2222-4000-8000-000000000001"),
    grupo_b=UUID("bbbbbbbb-2222-4000-8000-000000000001"),
    unidade_a1=UUID("aaaaaaaa-3333-4000-8000-000000000001"),
    unidade_b1=UUID("bbbbbbbb-3333-4000-8000-000000000001"),
    admin_a=UUID("aaaaaaaa-1111-4000-8000-000000000001"),
    responsavel_a1=UUID("aaaaaaaa-1111-4000-8000-000000000002"),
    admin_b=UUID("bbbbbbbb-1111-4000-8000-000000000001"),
    responsavel_b1=UUID("bbbbbbbb-1111-4000-8000-000000000002"),
    inventario_a1=UUID("aaaaaaaa-4444-4000-8000-000000000001"),
    inventario_b1=UUID("bbbbbbbb-4444-4000-8000-000000000001"),
    fonte_a1=UUID("aaaaaaaa-5555-4000-8000-000000000001"),
    fonte_b1=UUID("bbbbbbbb-5555-4000-8000-000000000001"),
    lancamento_a1=UUID("aaaaaaaa-6666-4000-8000-000000000001"),
    lancamento_b1=UUID("bbbbbbbb-6666-4000-8000-000000000001"),
    verificador_a=UUID("aaaaaaaa-7777-4000-8000-000000000001"),
    verificador_b=UUID("bbbbbbbb-7777-4000-8000-000000000001"),
    concessao_a1=UUID("aaaaaaaa-8888-4000-8000-000000000001"),
    concessao_b1=UUID("bbbbbbbb-8888-4000-8000-000000000001"),
)


def _dsn(conteiner: PostgresContainer, usuario: str, senha: str) -> str:
    host = conteiner.get_container_host_ip()
    porta = conteiner.get_exposed_port(5432)
    return f"postgresql://{usuario}:{senha}@{host}:{porta}/{conteiner.dbname}"


async def _preparar_papeis(dsn_admin: str) -> None:
    conexao = await asyncpg.connect(dsn_admin)
    try:
        await conexao.execute(PAPEIS_SQL.read_text(encoding="utf-8"))
        for papel, senha in (
            ("app_owner", SENHA_OWNER),
            ("app_user", SENHA_USER),
            ("app_curadoria", SENHA_CURADORIA),
        ):
            comando = await conexao.fetchval(
                "SELECT format('ALTER ROLE %I PASSWORD %L', $1::text, $2::text)", papel, senha
            )
            await conexao.execute(comando)
    finally:
        await conexao.close()


async def _semear_coleta(conexao: asyncpg.Connection) -> None:
    """Um inventario, uma fonte, um lancamento aprovado, uma planilha e uma evidencia por tenant."""
    por_tenant = (
        (DADOS.tenant_a, DADOS.empresa_a1, DADOS.unidade_a1, DADOS.inventario_a1, DADOS.fonte_a1,
         DADOS.lancamento_a1, DADOS.responsavel_a1, DADOS.admin_a),
        (DADOS.tenant_b, DADOS.empresa_b1, DADOS.unidade_b1, DADOS.inventario_b1, DADOS.fonte_b1,
         DADOS.lancamento_b1, DADOS.responsavel_b1, DADOS.admin_b),
    )  # fmt: skip
    for tenant, empresa, unidade, inventario, fonte, lancamento, autor, revisor in por_tenant:
        await conexao.execute(
            "INSERT INTO inventario (tenant_id, inventario_id, empresa_id, ano, abordagem)"
            " VALUES ($1, $2, $3, 2025, 'controle_operacional')",
            tenant,
            inventario,
            empresa,
        )
        await conexao.execute(
            "INSERT INTO fonte_emissao (tenant_id, fonte_id, empresa_id, inventario_id, unidade_id,"
            " categoria, descricao, unidade_medida, periodicidade)"
            " VALUES ($1, $2, $3, $4, $5, 'escopo1.combustao_estacionaria', 'Caldeira', 'L',"
            " 'mensal')",
            tenant,
            fonte,
            empresa,
            inventario,
            unidade,
        )
        await conexao.execute(
            "INSERT INTO planilha_importacao (tenant_id, planilha_id, empresa_id, inventario_id,"
            " nome_arquivo, sha256, chave_objeto, linhas_gravadas)"
            " VALUES ($1, gen_random_uuid(), $2, $3, 'coleta.csv', $4, 'x', 0)",
            tenant,
            empresa,
            inventario,
            bytes(32),
        )
        await conexao.execute(
            "INSERT INTO lancamento (tenant_id, lancamento_id, empresa_id, fonte_id, periodo,"
            " versao, quantidade, unidade, origem, criado_por)"
            " VALUES ($1, $2, $3, $4, 1, 1, 100, 'L', 'manual', $5)",
            tenant,
            lancamento,
            empresa,
            fonte,
            autor,
        )
        await conexao.execute(
            "INSERT INTO lancamento_decisao (tenant_id, lancamento_id, empresa_id, decisao,"
            " decidido_por) VALUES ($1, $2, $3, 'aprovado', $4)",
            tenant,
            lancamento,
            empresa,
            revisor,
        )
        await conexao.execute(
            "INSERT INTO evidencia (tenant_id, evidencia_id, empresa_id, fonte_id, periodo,"
            " nome_arquivo, tipo_conteudo, tamanho, sha256, chave_objeto)"
            " VALUES ($1, gen_random_uuid(), $2, $3, 1, 'nota.pdf', 'application/pdf', 10, $4,"
            " 'x')",
            tenant,
            empresa,
            fonte,
            bytes(32),
        )
        calculo = uuid4()
        await conexao.execute(
            "INSERT INTO calculo (tenant_id, calculo_id, empresa_id, inventario_id, metodo,"
            " metodo_versao, gwp_conjunto, conhecido_em, entrada, hash_entrada, hash_resultado,"
            " escopo1_tco2e, biogenico_tco2, linhas, pendencias) VALUES ($1, $2, $3, $4,"
            " 'fator_direto', '1.0.0', 'AR5_100', now(), '{}', $5, $5, 0, 0, 1, 1)",
            tenant,
            calculo,
            empresa,
            inventario,
            bytes(32),
        )
        await conexao.execute(
            "INSERT INTO calculo_linha (tenant_id, calculo_id, linha, empresa_id, fonte_id,"
            " lancamento_id, periodo, categoria, atividade, componente, data_referencia,"
            " quantidade, unidade, quantidade_convertida, gas, biogenico, fator, unidade_fator,"
            " fator_versao_id, gwp, gwp_versao_id, percentual_consolidado, massa_t, tco2e)"
            " VALUES ($1, $2, 1, $3, $4, $5, 1, 'escopo1.combustao_estacionaria', 'x', 'x',"
            " '2025-01-31', 100, 'L', 100, 'CO2', false, 0, 'kg/L', gen_random_uuid(), 1,"
            " gen_random_uuid(), 100, 0, 0)",
            tenant,
            calculo,
            empresa,
            fonte,
            lancamento,
        )
        await conexao.execute(
            "INSERT INTO calculo_pendencia (tenant_id, calculo_id, item, empresa_id, fonte_id,"
            " periodo, motivo, detalhe) VALUES ($1, $2, 1, $3, $4, 2, 'falta_dado', 'sem dado')",
            tenant,
            calculo,
            empresa,
            fonte,
        )
        meta = uuid4()
        await conexao.execute(
            "INSERT INTO meta (tenant_id, meta_id, empresa_id, nome, escopos, inventario_base_id,"
            " calculo_base_id, ano_base, valor_base_tco2e, ano_alvo, reducao_percentual)"
            " VALUES ($1, $2, $3, 'Meta 2030', '1+2', $4, $5, 2025, 100, 2030, 40)",
            tenant,
            meta,
            empresa,
            inventario,
            calculo,
        )
        await conexao.execute(
            "INSERT INTO acao_reducao (tenant_id, acao_id, empresa_id, meta_id, titulo,"
            " reducao_estimada_tco2e, ano_inicio) VALUES ($1, gen_random_uuid(), $2, $3,"
            " 'Trocar a caldeira', 10, 2026)",
            tenant,
            empresa,
            meta,
        )
        campanha = uuid4()
        await conexao.execute(
            "INSERT INTO campanha (tenant_id, campanha_id, empresa_id, inventario_id, prazo)"
            " VALUES ($1, $2, $3, $4, '2026-01-31')",
            tenant,
            campanha,
            empresa,
            inventario,
        )
        await conexao.execute(
            "INSERT INTO campanha_envio (tenant_id, campanha_id, tipo, empresa_id, destinatarios)"
            " VALUES ($1, $2, 'abertura', $3, 1)",
            tenant,
            campanha,
            empresa,
        )
        certificado = uuid4()
        await conexao.execute(
            "INSERT INTO certificado_assinatura (tenant_id, certificado_id, empresa_id, titular,"
            " emissor, numero_serie, valido_de, valido_ate, impressao_sha256, chave_cifrada,"
            " enviado_por) VALUES ($1, $2, $3, 'Titular', 'AC', '1', now(),"
            " now() + interval '1 year', $4, 'cl1.semente', $5)",
            tenant,
            certificado,
            empresa,
            bytes(32),
            revisor,
        )
        await conexao.execute(
            "INSERT INTO relatorio_inventario (tenant_id, relatorio_id, empresa_id,"
            " inventario_id, versao_fechamento, hash_fechamento, certificado_id, sha256,"
            " tamanho, chave_objeto, gerado_por) VALUES ($1, gen_random_uuid(), $2, $3, 1, $4,"
            " $5, $4, 100, 'semente', $6)",
            tenant,
            empresa,
            inventario,
            bytes(32),
            certificado,
            revisor,
        )
        await conexao.execute(
            "INSERT INTO relatorio_pedido (tenant_id, pedido_id, empresa_id, inventario_id,"
            " certificado_id, pedido_por) VALUES ($1, gen_random_uuid(), $2, $3, $4, $5)",
            tenant,
            empresa,
            inventario,
            certificado,
            revisor,
        )
        # Cobranca: assinatura com o teste ja acabado e uma fatura antiga paga, com um evento.
        await conexao.execute(
            "INSERT INTO assinatura (tenant_id, situacao, teste_ate, gateway)"
            " VALUES ($1, 'ativa', '2020-01-31', 'simulado')",
            tenant,
        )
        fatura = uuid4()
        await conexao.execute(
            "INSERT INTO fatura (tenant_id, fatura_id, competencia, empresas_ativas,"
            " valor_centavos, detalhamento, vencimento, situacao, gateway, gateway_cobranca_id,"
            " paga_em) VALUES ($1, $2, '2020-01-01', 1, 100, '[]', '2020-01-10', 'paga',"
            " 'simulado', $3, now())",
            tenant,
            fatura,
            f"semente_{fatura}",
        )
        await conexao.execute(
            "INSERT INTO cobranca_evento (tenant_id, evento_id, gateway, tipo, fatura_id)"
            " VALUES ($1, $2, 'simulado', 'pagamento_confirmado', $3)",
            tenant,
            f"semente_evento_{fatura}",
            fatura,
        )
        await conexao.execute(
            "INSERT INTO integridade_verificacao (tenant_id, verificacao_id, origem, iniciada_em,"
            " contagens, falhas, integra) VALUES ($1, gen_random_uuid(), 'agendada', now(),"
            " '{}', '[]', true)",
            tenant,
        )
        # Evento sem mudar o estado (o inventario segue em coleta): so para a suite de
        # vazamento ter linhas dos dois tenants.
        await conexao.execute(
            "INSERT INTO inventario_evento (tenant_id, evento_id, empresa_id, inventario_id, tipo,"
            " versao, motivo, registrado_por) VALUES ($1, gen_random_uuid(), $2, $3, 'reabertura',"
            " 1, 'Semente da suite de isolamento', $4)",
            tenant,
            empresa,
            inventario,
            revisor,
        )


async def _semear(dsn_admin: str) -> None:
    """Dados de referencia inseridos pelo superusuario (que ignora RLS)."""
    conexao = await asyncpg.connect(dsn_admin)
    try:
        await conexao.executemany(
            "INSERT INTO tenant (tenant_id, nome) VALUES ($1, $2)",
            [(DADOS.tenant_a, "Tenant A"), (DADOS.tenant_b, "Tenant B")],
        )
        await conexao.executemany(
            "INSERT INTO grupo (tenant_id, grupo_id, nome) VALUES ($1, $2, $3)",
            [
                (DADOS.tenant_a, DADOS.grupo_a, "Grupo A"),
                (DADOS.tenant_b, DADOS.grupo_b, "Grupo B"),
            ],
        )
        await conexao.executemany(
            "INSERT INTO empresa (tenant_id, empresa_id, grupo_id, nome) VALUES ($1, $2, $3, $4)",
            [
                (DADOS.tenant_a, DADOS.empresa_a1, DADOS.grupo_a, "Empresa A1"),
                (DADOS.tenant_a, DADOS.empresa_a2, DADOS.grupo_a, "Empresa A2"),
                (DADOS.tenant_b, DADOS.empresa_b1, DADOS.grupo_b, "Empresa B1"),
                (DADOS.tenant_b, DADOS.empresa_b2, DADOS.grupo_b, "Empresa B2"),
            ],
        )
        await conexao.executemany(
            "INSERT INTO unidade (tenant_id, unidade_id, empresa_id, nome, uf, municipio,"
            " controle_operacional, controle_financeiro) VALUES ($1, $2, $3, $4, 'SP', 'Campinas',"
            " true, true)",
            [
                (DADOS.tenant_a, DADOS.unidade_a1, DADOS.empresa_a1, "Fabrica A1"),
                (DADOS.tenant_b, DADOS.unidade_b1, DADOS.empresa_b1, "Fabrica B1"),
            ],
        )
        await conexao.executemany(
            "INSERT INTO usuario (usuario_id, email, nome) VALUES ($1, $2, $3)",
            [
                (DADOS.admin_a, "admin@a.example", "Admin A"),
                (DADOS.responsavel_a1, "resp@a.example", "Responsavel A1"),
                (DADOS.admin_b, "admin@b.example", "Admin B"),
                (DADOS.responsavel_b1, "resp@b.example", "Responsavel B1"),
                (DADOS.verificador_a, "verif@a.example", "Verificador A"),
                (DADOS.verificador_b, "verif@b.example", "Verificador B"),
            ],
        )
        await conexao.executemany(
            "INSERT INTO membro (tenant_id, usuario_id, perfil) VALUES ($1, $2, $3)",
            [
                (DADOS.tenant_a, DADOS.admin_a, "administrador"),
                (DADOS.tenant_a, DADOS.responsavel_a1, "responsavel"),
                (DADOS.tenant_b, DADOS.admin_b, "administrador"),
                (DADOS.tenant_b, DADOS.responsavel_b1, "responsavel"),
            ],
        )
        await conexao.executemany(
            "INSERT INTO membro_empresa (tenant_id, usuario_id, empresa_id) VALUES ($1, $2, $3)",
            [
                (DADOS.tenant_a, DADOS.responsavel_a1, DADOS.empresa_a1),
                (DADOS.tenant_b, DADOS.responsavel_b1, DADOS.empresa_b1),
            ],
        )
        await conexao.executemany(
            "INSERT INTO convite (tenant_id, convite_id, email, perfil, segredo_hash, expira_em)"
            " VALUES ($1, $2, $3, 'gestor', $4, now() + interval '7 days')",
            [
                (DADOS.tenant_a, uuid4(), "gestor@a.example", bytes(32)),
                (DADOS.tenant_b, uuid4(), "gestor@b.example", bytes(32)),
            ],
        )
        await _semear_coleta(conexao)
        # Concessao aceita, de 30 dias, para o inventario da empresa 1 de cada tenant.
        await conexao.executemany(
            "INSERT INTO concessao_verificacao (tenant_id, concessao_id, empresa_id,"
            " inventario_id, email, segredo_hash, criado_por, expira_em, aceito_em, usuario_id)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, now() + interval '30 days', now(), $8)",
            [
                (DADOS.tenant_a, DADOS.concessao_a1, DADOS.empresa_a1, DADOS.inventario_a1,
                 "verif@a.example", bytes(32), DADOS.admin_a, DADOS.verificador_a),
                (DADOS.tenant_b, DADOS.concessao_b1, DADOS.empresa_b1, DADOS.inventario_b1,
                 "verif@b.example", bytes(32), DADOS.admin_b, DADOS.verificador_b),
            ],
        )  # fmt: skip
        # seq e hash vem da trigger; os valores mandados aqui sao descartados.
        await conexao.executemany(
            "INSERT INTO audit_log (tenant_id, seq, evento_id, ocorrido_em, acao, entidade,"
            " hash_anterior, hash) VALUES ($1, 0, gen_random_uuid(), now(), $2, 'tenant',"
            " ''::bytea, ''::bytea)",
            [
                (DADOS.tenant_a, "tenant.criado"),
                (DADOS.tenant_a, "convite.criado"),
                (DADOS.tenant_b, "tenant.criado"),
            ],
        )
    finally:
        await conexao.close()


def migrar(dsn_owner: str, *argumentos: str) -> subprocess.CompletedProcess[str]:
    """Roda o Alembic de verdade, em outro processo, como app_owner."""
    ambiente = {**os.environ, "MIGRACAO_DATABASE_URL": dsn_owner}
    return subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-m", "alembic", *argumentos],
        cwd=RAIZ,
        env=ambiente,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="session")
def banco() -> Iterator[Banco]:
    with PostgresContainer("postgres:16", driver=None) as conteiner:
        dsn_admin = _dsn(conteiner, conteiner.username, conteiner.password)
        asyncio.run(_preparar_papeis(dsn_admin))
        dsn_owner = _dsn(conteiner, "app_owner", SENHA_OWNER)
        resultado = migrar(dsn_owner, "upgrade", "head")
        assert resultado.returncode == 0, resultado.stderr
        asyncio.run(_semear(dsn_admin))
        yield Banco(
            dsn_admin=dsn_admin,
            dsn_owner=dsn_owner,
            dsn_app=_dsn(conteiner, "app_user", SENHA_USER),
            dsn_curadoria=_dsn(conteiner, "app_curadoria", SENHA_CURADORIA),
        )


@pytest.fixture
def dados() -> Dados:
    return DADOS


@pytest.fixture
async def conexao_app(banco: Banco) -> AsyncIterator[asyncpg.Connection]:
    conexao = await asyncpg.connect(banco.dsn_app)
    try:
        yield conexao
    finally:
        await conexao.close()


@pytest.fixture
async def conexao_admin(banco: Banco) -> AsyncIterator[asyncpg.Connection]:
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        yield conexao
    finally:
        await conexao.close()


# ---------------------------------------------------------------- portal de ponta a ponta


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    from tests.isolation.portal import subir_redis

    yield from subir_redis()


@pytest.fixture(scope="session")
def emissor_oidc() -> Iterator[str]:
    from tests.isolation.portal import subir_oidc

    yield from subir_oidc()


@pytest.fixture
def emails(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    enviados: list[dict[str, str]] = []

    async def capturar(_configuracao: object, para: str, assunto: str, corpo: str) -> None:
        enviados.append({"para": para, "assunto": assunto, "corpo": corpo})

    monkeypatch.setattr("app.core.email.enviar_email", capturar)
    return enviados


@pytest.fixture
def app_cliente(
    banco: Banco, redis_url: str, emissor_oidc: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    from app.core.config import obter_configuracao
    from app.main import criar_app
    from tests.isolation.portal import AntivirusFalso, ArmazemMemoria, rotas_teste

    for nome, valor in {
        "DATABASE_URL": banco.dsn_app,
        "REDIS_URL": redis_url,
        "OIDC_DEV_EMISSOR": emissor_oidc,
        "COOKIE_SEGURO": "false",
        "APP_URL_BASE": "http://testserver",
        # Os roteiros entram muitas vezes pelo mesmo IP; o limite tem teste proprio.
        "LOGIN_LIMITE_TENTATIVAS": "100000",
        # Chave local de teste (32 bytes zero), para o certificado e a assinatura (P19).
        "PROVEDOR_CHAVES": "local",
        "CHAVES_LOCAIS": "teste-1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        # Cobranca com tabela de teste e o gateway simulado (P22).
        "COBRANCA_TABELA": "3:390,00;*:190,00",
        "COBRANCA_WEBHOOK_TOKEN": "token-de-teste",
    }.items():
        monkeypatch.setenv(nome, valor)
    obter_configuracao.cache_clear()
    app = criar_app()
    app.include_router(rotas_teste)
    app.state.armazem = ArmazemMemoria()
    app.state.antivirus = AntivirusFalso()

    # Relatorio: em vez do Celery, a tarefa roda na hora, pelo mesmo codigo do worker (conexao
    # asyncpg propria, depois do commit da requisicao), com as chaves e o S3 falso do app.
    from app.api import relatorios_telas
    from app.workers import relatorios as tarefa_relatorio

    monkeypatch.setattr(
        tarefa_relatorio, "dependencias", lambda: (app.state.chaves, app.state.armazem, None)
    )

    def enfileirar_na_hora(*, tenant_id: Any, pedido_id: Any) -> None:
        tarefa_relatorio.gerar_pedido(tenant_id=str(tenant_id), pedido_id=str(pedido_id))

    monkeypatch.setattr(relatorios_telas, "enfileirar_relatorio", enfileirar_na_hora)
    with TestClient(app, follow_redirects=False) as cliente:
        yield cliente
    obter_configuracao.cache_clear()
