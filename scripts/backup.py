"""Backup e ensaio de restauracao da pilha local (docker compose), parte local do P27.

    uv run python scripts/backup.py criar [--pasta backups] [--manter 7]
    uv run python scripts/backup.py ensaio [--pasta backups] [--backup NOME] [--manter-copia]
    uv run python scripts/backup.py listar [--pasta backups]

criar: pg_dump do banco inteiro (formato custom, dentro do container do Postgres) e copia de
todos os objetos do bucket (mc mirror, dentro do container do cliente do MinIO). Cada arquivo e
cifrado com a chave da plataforma (cifra envelope de app/core/keys.py, contexto com o id do
backup e o nome do arquivo) e entra no manifesto com o SHA-256 do conteudo original.

ensaio: decifra e confere tudo contra o manifesto, restaura num banco e num bucket descartaveis
(<banco>_ensaio e ensaio-<id>), compara a contagem de linhas tabela a tabela com o banco de
origem e roda a verificacao de integridade de todos os tenants na copia. Sai com 1 se algo nao
bater. A copia e apagada no fim, salvo --manter-copia.

Segredos nao passam pela linha de comando: o que precisa de senha roda dentro dos containers,
com as variaveis que eles ja tem. Sem a chave (CHAVES_LOCAIS), o backup nao abre: guarde-a
separada dos backups. Ver docs/runbooks/backup-e-restauracao.md.
"""

import argparse
import hashlib
import json
import shutil
import subprocess  # nosec B404  # docker compose com lista de argumentos, sem shell
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app.core import keys  # noqa: E402

FORMATO = 1
FINALIDADE = "backup"
DONO_DA_CIFRA = "plataforma"
ARQUIVO_MANIFESTO = "manifesto.json"
ARQUIVO_BANCO = "banco.dump.cl1"
PASTA_OBJETOS = "objetos"

# Roda um comando do docker compose e devolve a saida; recebe a entrada, se houver.
Executor = Callable[[Sequence[str], bytes | None], bytes]


class ErroBackup(RuntimeError):
    """Backup ou ensaio que nao pode seguir. A mensagem diz o que fazer."""


@dataclass(frozen=True)
class ConteudoDoBackup:
    manifesto: dict[str, Any]
    banco: bytes
    objetos: dict[str, bytes]


def _sha256(dados: bytes) -> str:
    return hashlib.sha256(dados).hexdigest()


def contexto(backup_id: str, arquivo: str) -> Mapping[str, str]:
    """Amarra cada arquivo cifrado ao backup e ao proprio nome: nao da para trocar de lugar."""
    return {
        "tenant_id": DONO_DA_CIFRA,
        "finalidade": FINALIDADE,
        "backup": backup_id,
        "arquivo": arquivo,
    }


def novo_id(agora: datetime | None = None) -> str:
    return (agora or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def gravar_backup(
    pasta: Path,
    provedor: keys.ProvedorChaves,
    backup_id: str,
    revisao: str,
    banco: bytes,
    objetos: Mapping[str, bytes],
) -> Path:
    """Grava o backup cifrado e o manifesto. Pasta nova: nunca sobrescreve um backup."""
    destino = pasta / backup_id
    destino.mkdir(parents=True, exist_ok=False)
    (destino / ARQUIVO_BANCO).write_text(
        keys.cifrar(provedor, banco, contexto(backup_id, ARQUIVO_BANCO)), encoding="ascii"
    )
    (destino / PASTA_OBJETOS).mkdir()
    lista = []
    for indice, chave in enumerate(sorted(objetos)):
        nome = f"{PASTA_OBJETOS}/{indice:06d}.cl1"
        conteudo = objetos[chave]
        (destino / nome).write_text(
            keys.cifrar(provedor, conteudo, contexto(backup_id, nome)), encoding="ascii"
        )
        lista.append(
            {"chave": chave, "arquivo": nome, "sha256": _sha256(conteudo), "tamanho": len(conteudo)}
        )
    manifesto = {
        "formato": FORMATO,
        "backup": backup_id,
        "revisao_banco": revisao,
        "banco": {"arquivo": ARQUIVO_BANCO, "sha256": _sha256(banco), "tamanho": len(banco)},
        "objetos": lista,
    }
    (destino / ARQUIVO_MANIFESTO).write_text(
        json.dumps(manifesto, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return destino


def ler_backup(pasta_backup: Path, provedor: keys.ProvedorChaves) -> ConteudoDoBackup:
    """Decifra e confere cada arquivo contra o manifesto. Qualquer diferenca para tudo."""
    try:
        manifesto = json.loads((pasta_backup / ARQUIVO_MANIFESTO).read_text(encoding="utf-8"))
    except (OSError, ValueError) as erro:
        raise ErroBackup(f"manifesto ausente ou ilegivel em {pasta_backup}") from erro
    if manifesto.get("formato") != FORMATO:
        raise ErroBackup(f"formato de backup desconhecido: {manifesto.get('formato')!r}")
    backup_id = manifesto["backup"]

    def abrir(arquivo: str, sha256: str) -> bytes:
        try:
            envelope = (pasta_backup / arquivo).read_text(encoding="ascii")
            dados = keys.decifrar(provedor, envelope, contexto(backup_id, arquivo))
        except (OSError, keys.ErroChave) as erro:
            raise ErroBackup(f"{arquivo}: nao abre ({erro}); confira a chave e o arquivo") from erro
        if _sha256(dados) != sha256:
            raise ErroBackup(f"{arquivo}: SHA-256 diferente do manifesto")
        return dados

    banco = abrir(manifesto["banco"]["arquivo"], manifesto["banco"]["sha256"])
    objetos = {o["chave"]: abrir(o["arquivo"], o["sha256"]) for o in manifesto["objetos"]}
    esperados = {o["arquivo"] for o in manifesto["objetos"]}
    presentes = {f"{PASTA_OBJETOS}/{p.name}" for p in (pasta_backup / PASTA_OBJETOS).glob("*.cl1")}
    if presentes != esperados:
        raise ErroBackup("a pasta de objetos nao bate com o manifesto (arquivo a mais ou a menos)")
    return ConteudoDoBackup(manifesto, banco, objetos)


def backups(pasta: Path) -> list[Path]:
    """Backups completos (com manifesto), do mais antigo ao mais novo."""
    if not pasta.is_dir():
        return []
    return sorted(p for p in pasta.iterdir() if (p / ARQUIVO_MANIFESTO).is_file())


def podar(pasta: Path, manter: int) -> list[Path]:
    """Apaga os backups mais antigos, mantendo os `manter` mais novos."""
    if manter < 1:
        raise ErroBackup("mantenha pelo menos 1 backup")
    antigos = backups(pasta)[:-manter]
    for antigo in antigos:
        shutil.rmtree(antigo)
    return antigos


def ler_contagens(saida: bytes) -> dict[str, int]:
    contagens: dict[str, int] = {}
    for linha in saida.decode("utf-8").splitlines():
        if "|" in linha:
            tabela, total = linha.split("|", 1)
            contagens[tabela.strip()] = int(total.strip())
    return contagens


def diferencas(origem: Mapping[str, int], copia: Mapping[str, int]) -> list[str]:
    """Tabelas cuja contagem nao bate (ou que existem so de um lado)."""
    return [
        f"{tabela}: origem {origem.get(tabela)}, copia {copia.get(tabela)}"
        for tabela in sorted(set(origem) | set(copia))
        if origem.get(tabela) != copia.get(tabela)
    ]


# ---------------------------------------------------------------- docker compose


def executor_compose(argumentos: Sequence[str], entrada: bytes | None) -> bytes:
    docker = shutil.which("docker")
    if docker is None:
        raise ErroBackup("docker nao encontrado no PATH; abra o Docker Desktop")
    resultado = subprocess.run(  # noqa: S603  # nosec B603
        [docker, "compose", *argumentos], input=entrada, capture_output=True, cwd=RAIZ
    )
    if resultado.returncode != 0:
        erro = resultado.stderr.decode("utf-8", "replace").strip().splitlines()[-5:]
        raise ErroBackup(f"docker compose {argumentos[0]} falhou: " + " | ".join(erro))
    return resultado.stdout


def _psql(executar: Executor, banco_sh: str, sql: str) -> bytes:
    """psql como superusuario, dentro do container; banco_sh e expandido pelo shell de la."""
    return executar(
        ["exec", "-T", "postgres", "sh", "-c",
         f'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "{banco_sh}" -tA -F "|"'],
        sql.encode("utf-8"),
    )  # fmt: skip


SQL_CONTAGENS = """
SELECT table_name, (xpath('/row/c/text()', query_to_xml(
    format('SELECT count(*) AS c FROM public.%I', table_name), false, true, '')))[1]::text
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY 1;
"""

_MC = 'mc alias set local http://minio:9000 "$S3_ACCESS_KEY" "$S3_SECRET_KEY" >/dev/null && '


def _mc(executar: Executor, montagem: str, comando: str) -> bytes:
    return executar(
        ["run", "--rm", "--no-deps", "-v", montagem, "--entrypoint", "/bin/sh", "minio-init",
         "-c", _MC + comando],
        None,
    )  # fmt: skip


def _ler_pasta(pasta: Path) -> dict[str, bytes]:
    return {
        arquivo.relative_to(pasta).as_posix(): arquivo.read_bytes()
        for arquivo in sorted(pasta.rglob("*"))
        if arquivo.is_file()
    }


def criar(pasta: Path, provedor: keys.ProvedorChaves, executar: Executor, manter: int) -> Path:
    backup_id = novo_id()
    revisao = _psql(executar, "$POSTGRES_DB", "SELECT version_num FROM alembic_version;")
    banco = executar(
        ["exec", "-T", "postgres", "sh", "-c",
         'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc'],
        None,
    )  # fmt: skip
    if not banco.startswith(b"PGDMP"):
        raise ErroBackup("o pg_dump nao devolveu um arquivo no formato custom")
    pasta.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=pasta, prefix=".objetos-") as temporaria:
        Path(temporaria).chmod(0o777)  # o usuario do container do mc escreve aqui
        _mc(
            executar,
            f"{Path(temporaria).resolve().as_posix()}:/saida",
            'mc mirror --quiet "local/$S3_BUCKET_EVIDENCIAS" /saida',
        )
        objetos = _ler_pasta(Path(temporaria))
    destino = gravar_backup(
        pasta, provedor, backup_id, revisao.decode("utf-8").strip(), banco, objetos
    )
    removidos = podar(pasta, manter)
    print(f"Backup {backup_id}: banco de {len(banco)} bytes e {len(objetos)} objeto(s).")
    print(f"Guardado em {destino}. Backups antigos apagados: {len(removidos)}.")
    return destino


def ensaio(
    pasta_backup: Path, provedor: keys.ProvedorChaves, executar: Executor, manter_copia: bool
) -> int:
    conteudo = ler_backup(pasta_backup, provedor)
    backup_id = conteudo.manifesto["backup"]
    print(f"Backup {backup_id} aberto e conferido: {len(conteudo.objetos)} objeto(s).")
    banco_origem = executar(
        ["exec", "-T", "postgres", "sh", "-c", 'printf %s "$POSTGRES_DB"'], None
    ).decode("utf-8")
    banco = f"{banco_origem}_ensaio"
    bucket = f"ensaio-{backup_id.lower()}"
    falhas: list[str] = []
    try:
        executar(
            ["exec", "-T", "postgres", "sh", "-c",
             f'dropdb --if-exists -U "$POSTGRES_USER" {banco}'
             f' && createdb -U "$POSTGRES_USER" -O app_owner {banco}'],
            None,
        )  # fmt: skip
        executar(
            ["exec", "-T", "postgres", "sh", "-c",
             f'pg_restore -U "$POSTGRES_USER" -d {banco} --exit-on-error'],
            conteudo.banco,
        )  # fmt: skip
        # As permissoes de banco (CONNECT) nao vem no dump: as mesmas do infra/sql/papeis.sql.
        _psql(
            executar,
            banco,
            f"REVOKE ALL ON DATABASE {banco} FROM PUBLIC;"
            f" GRANT CONNECT, TEMPORARY ON DATABASE {banco} TO app_owner;"
            f" GRANT CONNECT ON DATABASE {banco} TO app_user;"
            f" GRANT CONNECT ON DATABASE {banco} TO app_curadoria;",
        )
        revisao = _psql(executar, banco, "SELECT version_num FROM alembic_version;")
        if revisao.decode("utf-8").strip() != conteudo.manifesto["revisao_banco"]:
            falhas.append("revisao do banco restaurado diferente da do manifesto")
        origem = ler_contagens(_psql(executar, "$POSTGRES_DB", SQL_CONTAGENS))
        copia = ler_contagens(_psql(executar, banco, SQL_CONTAGENS))
        print(f"Banco restaurado em {banco}: {len(copia)} tabela(s).")
        if diferencas(origem, copia):
            print("Contagens diferentes (normal se o app gravou algo depois do backup):")
            for item in diferencas(origem, copia):
                print(f"    {item}")
        with tempfile.TemporaryDirectory(dir=pasta_backup.parent, prefix=".ensaio-") as temp:
            for chave, dados in conteudo.objetos.items():
                arquivo = Path(temp) / chave
                arquivo.parent.mkdir(parents=True, exist_ok=True)
                arquivo.write_bytes(dados)
            _mc(
                executar,
                f"{Path(temp).resolve().as_posix()}:/entrada:ro",
                f"mc mb --ignore-existing local/{bucket} >/dev/null"
                f" && mc mirror --quiet /entrada local/{bucket}",
            )
        print(f"Objetos restaurados no bucket {bucket}.")
        try:
            saida = executar(
                ["run", "--rm", "--no-deps", "api", "python", "-m", "app.cli", "integridade",
                 "verificar", "--banco", banco, "--bucket", bucket],
                None,
            )  # fmt: skip
            print(saida.decode("utf-8").rstrip())
        except ErroBackup as erro:
            falhas.append(f"verificacao de integridade da copia: {erro}")
    finally:
        if not manter_copia:
            executar(
                ["exec", "-T", "postgres", "sh", "-c",
                 f'dropdb --if-exists -U "$POSTGRES_USER" {banco}'],
                None,
            )  # fmt: skip
            _mc(executar, f"{pasta_backup.resolve().as_posix()}:/nada:ro",
                f"mc rb --force local/{bucket} >/dev/null 2>&1 || true")  # fmt: skip
            print("Copia descartada (banco e bucket do ensaio apagados).")
        else:
            print(f"Copia mantida: banco {banco}, bucket {bucket}.")
    if falhas:
        print("ENSAIO COM FALHA:")
        for falha in falhas:
            print(f"    {falha}")
        return 1
    print(f"Ensaio de restauracao do backup {backup_id}: ok.")
    return 0


def _provedor() -> keys.ProvedorChaves:
    from app.core.config import obter_configuracao

    provedor = keys.provedor_da_configuracao(obter_configuracao())
    if provedor is None:
        raise ErroBackup("sem chave da plataforma: defina CHAVES_LOCAIS no .env")
    return provedor


def main(argumentos: list[str] | None = None) -> int:
    analisador = argparse.ArgumentParser(prog="scripts/backup.py")
    analisador.add_argument("--pasta", type=Path, default=RAIZ / "backups")
    comandos = analisador.add_subparsers(dest="comando", required=True)
    novo = comandos.add_parser("criar", help="backup cifrado do banco e dos objetos")
    novo.add_argument("--manter", type=int, default=7, help="quantos backups manter")
    restaurar = comandos.add_parser("ensaio", help="restaura numa copia descartavel e confere")
    restaurar.add_argument("--backup", help="nome da pasta do backup (padrao: o mais novo)")
    restaurar.add_argument("--manter-copia", action="store_true")
    comandos.add_parser("listar", help="lista os backups")
    args = analisador.parse_args(argumentos)
    try:
        if args.comando == "listar":
            for item in backups(args.pasta):
                manifesto = json.loads((item / ARQUIVO_MANIFESTO).read_text(encoding="utf-8"))
                print(f"{item.name}  revisao {manifesto['revisao_banco']}"
                      f"  objetos {len(manifesto['objetos'])}")  # fmt: skip
            return 0
        if args.comando == "criar":
            criar(args.pasta, _provedor(), executor_compose, args.manter)
            return 0
        existentes = backups(args.pasta)
        if not existentes:
            raise ErroBackup(f"nenhum backup em {args.pasta}; rode 'criar' primeiro")
        escolhido = args.pasta / args.backup if args.backup else existentes[-1]
        return ensaio(escolhido, _provedor(), executor_compose, args.manter_copia)
    except ErroBackup as erro:
        print(f"Erro: {erro}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
