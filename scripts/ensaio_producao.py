"""Ensaio de producao local: sobe infra/ensaio-producao e confere a lista de entrada no ar.

    uv run python scripts/ensaio_producao.py subir      # monta, sobe, espera e confere
    uv run python scripts/ensaio_producao.py conferir   # so confere (pilha ja de pe)
    uv run python scripts/ensaio_producao.py derrubar   # derruba e apaga os volumes

Cada item e conferido com evidencia (resposta HTTP, consulta no banco, docker inspect, chamada
de dentro do container), nunca por suposicao. Sai com 1 se algum item falhar.
Ver docs/runbooks/ensaio-de-producao.md.
"""

import argparse
import shutil
import subprocess  # nosec B404  # docker compose com lista de argumentos, sem shell
import sys
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parents[1]
ARQUIVO_COMPOSE = RAIZ / "infra" / "ensaio-producao" / "docker-compose.yml"
URL = "https://localhost:8443"
SERVICOS_DO_APP = ("api", "worker", "beat")


class ErroEnsaio(RuntimeError):
    pass


@dataclass(frozen=True)
class Resultado:
    item: str
    ok: bool
    detalhe: str


def compose(*argumentos: str, entrada: bytes | None = None, checar: bool = True) -> str:
    docker = shutil.which("docker")
    if docker is None:
        raise ErroEnsaio("docker nao encontrado; abra o Docker Desktop")
    resultado = subprocess.run(  # noqa: S603  # nosec B603
        [docker, "compose", "-f", str(ARQUIVO_COMPOSE), "--env-file", str(RAIZ / ".env"),
         *argumentos],
        input=entrada,
        capture_output=True,
        cwd=RAIZ,
    )  # fmt: skip
    if checar and resultado.returncode != 0:
        erro = resultado.stderr.decode("utf-8", "replace").strip().splitlines()[-5:]
        raise ErroEnsaio(f"docker compose {argumentos[0]}: " + " | ".join(erro))
    return resultado.stdout.decode("utf-8", "replace")


def docker_inspect(servico: str, formato: str) -> str:
    conteiner = compose("ps", "-q", servico).strip()
    if not conteiner:
        raise ErroEnsaio(f"servico {servico} nao esta de pe")
    docker = shutil.which("docker") or "docker"
    return (
        subprocess.run(  # noqa: S603  # nosec B603
            [docker, "inspect", conteiner, "--format", formato],
            capture_output=True,
            check=True,
        )
        .stdout.decode("utf-8")
        .strip()
    )


def no_app(codigo: str, servico: str = "api") -> str:
    """Roda Python dentro de um container do app (com a configuracao de producao dele)."""
    return compose("exec", "-T", servico, "python", "-c", codigo).strip()


# ---------------------------------------------------------------- itens


def _https() -> Resultado:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        resposta = httpx.get(f"{URL}/saude", verify=False, timeout=10)  # noqa: S501  # nosec B501  # CA interna do Caddy
    cabecalhos = resposta.headers
    faltando = [
        nome
        for nome in (
            "strict-transport-security",
            "content-security-policy",
            "x-content-type-options",
        )
        if nome not in cabecalhos
    ]
    ok = resposta.status_code == 200 and not faltando
    return Resultado("HTTPS com HSTS, CSP e nosniff", ok,
                     f"status {resposta.status_code}; faltando: {faltando or 'nada'}")  # fmt: skip


def _http_sem_tls() -> Resultado:
    try:
        resposta = httpx.get("http://localhost:8443/saude", timeout=10)
        ok, detalhe = resposta.status_code >= 400, f"status {resposta.status_code}"
    except httpx.HTTPError as erro:
        ok, detalhe = True, f"recusado ({type(erro).__name__})"
    return Resultado("HTTP sem TLS recusado", ok, detalhe)


def _travas_de_producao() -> Resultado:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ui = httpx.get(f"{URL}/ui", verify=False, timeout=10,  # noqa: S501  # nosec B501
                       headers={"accept": "application/json"})  # fmt: skip
        entrar = httpx.get(f"{URL}/entrar", verify=False, timeout=10)  # noqa: S501  # nosec B501
    ok = ui.status_code == 404 and "/entrar/dev" not in entrar.text
    estado = "ausente" if "/entrar/dev" not in entrar.text else "presente"
    return Resultado(
        "Galeria /ui e login de dev ausentes", ok, f"/ui {ui.status_code}; login de dev {estado}"
    )


def _banco_com_tls() -> Resultado:
    saida = no_app(
        "import asyncio, asyncpg\n"
        "from app.core.config import obter_configuracao\n"
        "from app.core.db import dsn_asyncpg\n"
        "async def main():\n"
        "    url = dsn_asyncpg(obter_configuracao().database_url)\n"
        "    c = await asyncpg.connect(url)\n"
        "    tls = await c.fetchval('SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()')\n"
        "    await c.close()\n"
        "    try:\n"
        "        s = await asyncpg.connect(url.replace('sslmode=require', 'sslmode=disable'))\n"
        "        await s.close(); sem = 'aceita'\n"
        "    except Exception as e:\n"
        "        sem = 'recusada:' + type(e).__name__\n"
        "    print(f'{tls} {sem}')\n"
        "asyncio.run(main())\n"
    )
    ok = saida.startswith("True recusada")
    return Resultado("Banco so com TLS (app_user com TLS; sem TLS recusado)", ok, saida)


def _banco_fechado() -> Resultado:
    portas = docker_inspect("postgres", "{{json .HostConfig.PortBindings}}")
    return Resultado("Postgres sem porta no host", portas in ("{}", "null"), portas)


def _conteineres_endurecidos() -> Resultado:
    problemas = []
    for servico in SERVICOS_DO_APP:
        so_leitura = docker_inspect(servico, "{{.HostConfig.ReadonlyRootfs}}")
        usuario = docker_inspect(servico, "{{.Config.User}}")
        capacidades = docker_inspect(servico, "{{json .HostConfig.CapDrop}}")
        opcoes = docker_inspect(servico, "{{json .HostConfig.SecurityOpt}}")
        if so_leitura != "true":
            problemas.append(f"{servico}: raiz gravavel")
        if usuario in ("", "root", "0"):
            problemas.append(f"{servico}: roda como root")
        if "ALL" not in capacidades:
            problemas.append(f"{servico}: capacidades nao removidas")
        if "no-new-privileges" not in opcoes:
            problemas.append(f"{servico}: sem no-new-privileges")
    return Resultado("API, worker e beat sem root, raiz so leitura, sem capacidades",
                     not problemas, "; ".join(problemas) or "ok")  # fmt: skip


def _saude_dos_servicos() -> Resultado:
    estados = {s: docker_inspect(s, "{{.State.Health.Status}}") for s in SERVICOS_DO_APP}
    ok = all(estado == "healthy" for estado in estados.values())
    return Resultado("API, worker e beat saudaveis", ok, str(estados))


def _migracoes() -> Resultado:
    # Como superusuario, no container do banco: o app_user nao le a tabela do Alembic.
    no_banco = compose(
        "exec", "-T", "postgres", "sh", "-c",
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA'
        ' -c "SELECT version_num FROM alembic_version"',
    ).strip()  # fmt: skip
    ultima = sorted(
        p.name.split("_", 1)[0] for p in (RAIZ / "migrations" / "versions").glob("0*.py")
    )[-1]
    return Resultado("Banco na ultima migracao", no_banco == ultima,
                     f"banco {no_banco}, codigo {ultima}")  # fmt: skip


def _kms() -> Resultado:
    saida = no_app(
        "from app.core import keys\n"
        "from app.core.config import obter_configuracao\n"
        "p = keys.provedor_da_configuracao(obter_configuracao())\n"
        "c = {'tenant_id': 'ensaio', 'finalidade': 'ensaio'}\n"
        "e = keys.cifrar(p, b'ok', c)\n"
        "print(type(p).__name__, keys.decifrar(p, e, c) == b'ok')\n"
    )
    return Resultado("Cifra pelo KMS", saida == "ProvedorChavesKMS True", saida)


def _object_lock() -> Resultado:
    saida = no_app(
        "import asyncio, hashlib, uuid\n"
        "from botocore.exceptions import ClientError\n"
        "from app.core.armazenamento import ArmazemS3\n"
        "from app.core.config import obter_configuracao\n"
        "a = ArmazemS3(obter_configuracao())\n"
        "dados = b'ensaio de producao'\n"
        "g = asyncio.run(a.guardar(f'ensaio/{uuid.uuid4()}', dados,"
        " hashlib.sha256(dados).digest(), 'text/plain'))\n"
        "modo = a._cliente.get_object_retention(Bucket=a.bucket, Key=g.chave,"
        " VersionId=g.versao)['Retention']['Mode']\n"
        "try:\n"
        "    a._cliente.delete_object(Bucket=a.bucket, Key=g.chave, VersionId=g.versao)\n"
        "    apagou = 'apagou'\n"
        "except ClientError as e:\n"
        "    apagou = 'recusou:' + e.response['Error']['Code']\n"
        "print(modo, apagou)\n"
    )
    ok = saida.startswith("COMPLIANCE recusou")
    return Resultado("Objeto em COMPLIANCE, apagar recusado", ok, saida)


def _pdf() -> Resultado:
    # O relatorio sai no worker (relatorios.gerar_pedido): e la que o Chromium precisa rodar.
    saida = no_app(
        "import asyncio\n"
        "from app.modules.relatorios.relatorio import gerar_pdf\n"
        "html = '<!DOCTYPE html><html lang=\"pt-BR\"><body>ok</body></html>'\n"
        "pdf = asyncio.run(gerar_pdf(html))\n"
        "print(pdf[:5].decode(), len(pdf))\n",
        servico="worker",
    )
    return Resultado(
        "PDF gerado no worker (Chromium, raiz so leitura)", saida.startswith("%PDF-"), saida
    )


def _antivirus() -> Resultado:
    # A assinatura de teste montada em pedacos, para este arquivo nao disparar antivirus.
    saida = no_app(
        "import asyncio\n"
        "from app.core.antivirus import ArquivoInfectado, ClienteClamav\n"
        "from app.core.config import obter_configuracao\n"
        "c = obter_configuracao()\n"
        "eicar = ('X5O!P%@AP[4' + '\\\\PZX54(P^)7CC)7}$' + 'EICAR-STANDARD-ANTIVIRUS-' + "
        "'TEST-FILE!$H+H*').encode()\n"
        "av = ClienteClamav(c.clamav_host, c.clamav_port, c.clamav_tempo_limite_s)\n"
        "try:\n"
        "    asyncio.run(av.verificar(eicar)); print('passou')\n"
        "except ArquivoInfectado:\n"
        "    print('barrado')\n"
    )
    return Resultado("ClamAV barra a assinatura de teste", saida == "barrado", saida)


ITENS: tuple[Callable[[], Resultado], ...] = (
    _https, _http_sem_tls, _travas_de_producao, _banco_com_tls, _banco_fechado,
    _conteineres_endurecidos, _saude_dos_servicos, _migracoes, _kms, _object_lock, _pdf,
    _antivirus,
)  # fmt: skip


def conferir(itens: Sequence[Callable[[], Resultado]] = ITENS) -> int:
    resultados = []
    for item in itens:
        try:
            resultados.append(item())
        except Exception as erro:  # item que nem roda e falha, com o motivo
            resultados.append(Resultado(item.__name__.strip("_"), False, f"erro: {erro}"))
    for r in resultados:
        print(f"[{'ok' if r.ok else 'FALHA'}] {r.item}: {r.detalhe}")
    falhas = sum(1 for r in resultados if not r.ok)
    print(f"{len(resultados) - falhas} de {len(resultados)} itens ok.")
    return 1 if falhas else 0


def main(argumentos: list[str] | None = None) -> int:
    analisador = argparse.ArgumentParser(prog="scripts/ensaio_producao.py")
    analisador.add_argument("acao", choices=["subir", "conferir", "derrubar"])
    args = analisador.parse_args(argumentos)
    try:
        if args.acao == "derrubar":
            compose("down", "-v", "--remove-orphans")
            print("Ensaio derrubado e volumes apagados.")
            return 0
        if args.acao == "subir":
            print("Montando a imagem e subindo (o ClamAV leva alguns minutos na primeira vez)...")
            compose("up", "-d", "--build", "--wait", "--wait-timeout", "900")
        return conferir()
    except ErroEnsaio as erro:
        print(f"Erro: {erro}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
