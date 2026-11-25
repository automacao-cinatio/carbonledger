"""Comandos de operacao da CINATIO.

    python -m app.cli criar-tenant --nome "Cliente S.A." --email admin@cliente.com.br

Cria o tenant e um convite de administrador, e mostra os links do convite (um por provedor
habilitado). O operador repassa o link; o convite so vale para quem entrar com esse e-mail.

    python -m app.cli fatores importar --tipo fatores --por "Nome" data/fatores/arquivo.csv
    python -m app.cli fatores lotes
    python -m app.cli fatores aprovar LOTE --responsavel "Nome"
    python -m app.cli fatores rejeitar LOTE --responsavel "Nome" --motivo "por que"

Curadoria da referencia (fatores, GWP e misturas). Conecta com CURADORIA_DATABASE_URL (papel
app_curadoria). Nada importado vale para calculo antes de aprovado.

    python -m app.cli lgpd pendentes
    python -m app.cli lgpd anonimizar USUARIO --por "Nome de quem executa"

Pedidos de exclusao dos titulares (LGPD) e a anonimizacao, irreversivel. Ver
docs/runbooks/lgpd.md.

    python -m app.cli integridade verificar [--banco NOME] [--bucket NOME]

Verificacao de integridade de todos os tenants, na hora. --banco e --bucket apontam para uma
copia restaurada (ensaio de restauracao, docs/runbooks/backup-e-restauracao.md): trocam so o
nome do banco da DATABASE_URL e o bucket, sem senha na linha de comando. Sai com 1 se algum
tenant tiver falha.
"""

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4
from uuid import UUID as _UUID

from app.core import audit
from app.core.config import Configuracao, obter_configuracao
from app.core.db import dsn_asyncpg
from app.core.security.convite import novo_token
from app.core.security.oidc import provedores_habilitados
from app.core.tenancy import ContextoTenant, Escopo, conexao_de_tenant
from app.modules.fatores import repositorio
from app.modules.fatores.arquivos import ErroArquivo
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
        await audit.registrar_asyncpg(
            conexao,
            tenant_id=tenant_id,
            acao="tenant.criado",
            entidade="tenant",
            entidade_id=tenant_id,
        )
        await audit.registrar_asyncpg(
            conexao,
            tenant_id=tenant_id,
            acao="convite.criado",
            entidade="convite",
            entidade_id=token.convite_id,
            dados={"perfil": "administrador", "empresas": [], "origem": "linha_de_comando"},
        )
    return tenant_id, token.codificar()


async def _curadoria(configuracao: Configuracao, acao: str, args: argparse.Namespace) -> int:
    import asyncpg

    if not configuracao.curadoria_database_url:
        print("Defina CURADORIA_DATABASE_URL (papel app_curadoria) para a curadoria.")
        return 2
    conexao = await asyncpg.connect(dsn_asyncpg(configuracao.curadoria_database_url))
    try:
        if acao == "importar":
            caminho = Path(args.arquivo)
            try:
                resultado = await repositorio.importar(
                    conexao, args.tipo, caminho.as_posix(), caminho.read_bytes(), args.por
                )
            except ErroArquivo as erro:
                print(f"Arquivo recusado ({len(erro.problemas)} problema(s)); nada foi gravado:")
                for problema in erro.problemas:
                    print(f"  {problema}")
                return 1
            except repositorio.LoteRepetido as erro:
                print(f"Nada a fazer: {erro}.")
                return 0
            print(f"Lote {resultado.lote_id} importado, aguardando decisao.")
            print(f"  linhas novas: {resultado.inseridas}")
            print(f"  iguais a versoes ja aprovadas (ignoradas): {resultado.ja_conhecidas}")
            print(f"  SHA-256 do arquivo: {resultado.sha256}")
        elif acao == "lotes":
            for lote in await repositorio.listar_lotes(conexao):
                situacao = lote["decisao"] or "pendente"
                quando = f"{lote['importado_em']:%d/%m/%Y %H:%M}"
                print(
                    f"{lote['lote_id']}  {lote['tipo']:<8} {situacao:<9} {lote['linhas']:>5} linhas"
                    f"  {lote['arquivo']}  ({lote['importado_por']}, {quando})"
                )
        else:
            try:
                instante = await repositorio.decidir(
                    conexao,
                    _UUID(args.lote),
                    aprovar=acao == "aprovar",
                    responsavel=args.responsavel,
                    justificativa=getattr(args, "motivo", None),
                )
            except (repositorio.DecisaoInvalida, ValueError) as erro:
                print(f"Decisao recusada: {erro}.")
                return 1
            situacao = "aprovado" if acao == "aprovar" else "rejeitado"
            print(f"Lote {args.lote} {situacao} em {instante:%d/%m/%Y %H:%M:%S}.")
    finally:
        await conexao.close()
    return 0


async def _lgpd(configuracao: Configuracao, args: argparse.Namespace) -> int:
    import asyncpg

    from app.modules.lgpd import servico as lgpd

    conexao = await asyncpg.connect(dsn_asyncpg(configuracao.database_url))
    try:
        if args.acao == "pendentes":
            pedidos = await lgpd.pendentes(conexao)
            if not pedidos:
                print("Nenhum pedido de exclusao em aberto.")
            for pedido in pedidos:
                print(
                    f"{pedido['pedido_id']}  pessoa {pedido['usuario_id']}"
                    f"  pedido em {pedido['solicitado_em']:%d/%m/%Y %H:%M}"
                )
            return 0
        try:
            usuario = _UUID(args.usuario)
        except ValueError:
            print("Informe o id da pessoa (UUID), como aparece em 'lgpd pendentes'.")
            return 2
        try:
            tenants = await lgpd.anonimizar(conexao, usuario, args.por)
        except lgpd.PessoaJaAnonimizada:
            print("Nada a fazer: a pessoa ja foi anonimizada.")
            return 0
        print(f"Pessoa {usuario} anonimizada. Registrado na trilha de {len(tenants)} cliente(s).")
        return 0
    finally:
        await conexao.close()


def trocar_banco(database_url: str, banco: str | None) -> str:
    """A mesma URL com outro nome de banco (para verificar uma copia restaurada)."""
    if not banco:
        return database_url
    if not banco.replace("_", "").isalnum():
        raise ValueError("nome de banco invalido")
    partes = urlsplit(database_url)
    return urlunsplit(partes._replace(path=f"/{banco}"))


async def _integridade(configuracao: Configuracao, args: argparse.Namespace) -> int:
    import asyncpg

    from app.core.armazenamento import ArmazemS3
    from app.modules.integridade import servico as integridade

    if args.bucket:
        configuracao = configuracao.model_copy(update={"s3_bucket_evidencias": args.bucket})
    url = trocar_banco(configuracao.database_url, args.banco)
    armazem = ArmazemS3(configuracao)
    conexao = await asyncpg.connect(dsn_asyncpg(url))
    try:
        tenants = [linha[0] for linha in await conexao.fetch("SELECT identidade_tenants()")]
    finally:
        await conexao.close()
    com_falha = 0
    for tenant_id in tenants:
        contexto = ContextoTenant(tenant_id=tenant_id, escopo=Escopo.TENANT)
        async with conexao_de_tenant(url, contexto) as conexao_tenant:
            resultado = await integridade.verificar(
                conexao_tenant, tenant_id, armazem, origem="manual"
            )
        contagens = ", ".join(f"{k}={v}" for k, v in sorted(resultado.contagens.items()))
        situacao = "integro" if resultado.integra else f"{len(resultado.falhas)} falha(s)"
        print(f"{tenant_id}  {situacao}  ({contagens})")
        for falha in resultado.falhas:
            print(f"    {falha.tipo} {falha.referencia}: {falha.motivo}")
        com_falha += 0 if resultado.integra else 1
    print(f"{len(tenants)} tenant(s) verificado(s); {com_falha} com falha.")
    return 1 if com_falha else 0


def main(argumentos: list[str] | None = None) -> int:
    analisador = argparse.ArgumentParser(prog="python -m app.cli")
    comandos = analisador.add_subparsers(dest="comando", required=True)
    criar = comandos.add_parser("criar-tenant", help="cria um tenant e o convite do administrador")
    criar.add_argument("--nome", required=True)
    criar.add_argument("--email", required=True)

    fatores = comandos.add_parser("fatores", help="curadoria de fatores, GWP e misturas")
    acoes = fatores.add_subparsers(dest="acao", required=True)
    importar = acoes.add_parser("importar", help="valida e grava um arquivo como lote pendente")
    importar.add_argument("--tipo", required=True, choices=repositorio.TIPOS)
    importar.add_argument("--por", required=True, help="quem importa (nome)")
    importar.add_argument("arquivo")
    acoes.add_parser("lotes", help="lista os lotes e as decisoes")
    aprovar = acoes.add_parser("aprovar", help="aprova um lote (uma unica vez)")
    aprovar.add_argument("lote")
    aprovar.add_argument("--responsavel", required=True)
    rejeitar = acoes.add_parser("rejeitar", help="rejeita um lote, com justificativa")
    rejeitar.add_argument("lote")
    rejeitar.add_argument("--responsavel", required=True)
    rejeitar.add_argument("--motivo", required=True)
    lgpd = comandos.add_parser("lgpd", help="pedidos dos titulares e anonimizacao")
    acoes_lgpd = lgpd.add_subparsers(dest="acao", required=True)
    acoes_lgpd.add_parser("pendentes", help="lista os pedidos de exclusao em aberto")
    anonimizar = acoes_lgpd.add_parser("anonimizar", help="anonimiza a pessoa (irreversivel)")
    anonimizar.add_argument("usuario")
    anonimizar.add_argument("--por", required=True, help="quem executa (nome)")
    verificacao = comandos.add_parser("integridade", help="verificacao de integridade na hora")
    acoes_integridade = verificacao.add_subparsers(dest="acao", required=True)
    verificar = acoes_integridade.add_parser("verificar", help="verifica todos os tenants")
    verificar.add_argument("--banco", help="outro banco no mesmo servidor (copia restaurada)")
    verificar.add_argument("--bucket", help="outro bucket (copia restaurada)")
    args = analisador.parse_args(argumentos)

    configuracao = obter_configuracao()
    if args.comando == "fatores":
        return asyncio.run(_curadoria(configuracao, args.acao, args))
    if args.comando == "lgpd":
        return asyncio.run(_lgpd(configuracao, args))
    if args.comando == "integridade":
        return asyncio.run(_integridade(configuracao, args))

    tenant_id, token = asyncio.run(criar_tenant(configuracao, args.nome, args.email))
    base = configuracao.app_url_base.rstrip("/")
    print(f"Tenant criado: {tenant_id}")
    print("Links do convite de administrador (repasse so para a pessoa convidada):")
    for provedor in provedores_habilitados(configuracao):
        print(f"  {provedor}: {base}/convite/{token}/{provedor}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
