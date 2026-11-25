"""Backup local (scripts/backup.py): cifra, manifesto, adulteracao, poda e o roteiro do ensaio
com um docker compose falso. Tambem o fallback de versao do S3 e a troca de banco do CLI."""

import importlib.util
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.cli import trocar_banco
from app.core import keys
from app.core.armazenamento import ArmazemS3

_CAMINHO = Path(__file__).resolve().parents[2] / "scripts" / "backup.py"
_spec = importlib.util.spec_from_file_location("backup", _CAMINHO)
assert _spec is not None and _spec.loader is not None
backup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup)

PROVEDOR = keys.ProvedorChavesLocal.da_configuracao(
    "teste-1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)
OUTRO = keys.ProvedorChavesLocal.da_configuracao("outra:" + "B" * 43 + "=")
OBJETOS = {
    "tenants/t1/empresas/e1/evidencias/a": b"%PDF-1.7 nota",
    "tenants/t1/empresas/e1/relatorios/b": b"%PDF-1.7 relatorio",
}


def _gravar(pasta: Path, backup_id: str = "20261124T120000Z") -> Path:
    return backup.gravar_backup(  # type: ignore[no-any-return]
        pasta, PROVEDOR, backup_id, "0017", b"PGDMP banco", OBJETOS
    )


def test_ida_e_volta_confere_tudo(tmp_path: Path) -> None:
    destino = _gravar(tmp_path)
    manifesto = json.loads((destino / "manifesto.json").read_text(encoding="utf-8"))
    assert manifesto["revisao_banco"] == "0017" and len(manifesto["objetos"]) == 2
    # Nada em claro no disco: nem o banco, nem os objetos, nem as chaves dos objetos no nome.
    for arquivo in destino.rglob("*.cl1"):
        texto = arquivo.read_text(encoding="ascii")
        assert texto.startswith("cl1.") and "PDF" not in texto and "PGDMP" not in texto
    conteudo = backup.ler_backup(destino, PROVEDOR)
    assert conteudo.banco == b"PGDMP banco" and conteudo.objetos == OBJETOS


def test_nunca_sobrescreve_um_backup(tmp_path: Path) -> None:
    _gravar(tmp_path)
    with pytest.raises(FileExistsError):
        _gravar(tmp_path)


def test_chave_errada_nao_abre(tmp_path: Path) -> None:
    destino = _gravar(tmp_path)
    with pytest.raises(backup.ErroBackup, match="nao abre"):
        backup.ler_backup(destino, OUTRO)


def test_arquivos_trocados_de_lugar_nao_abrem(tmp_path: Path) -> None:
    destino = _gravar(tmp_path)
    a, b = destino / "objetos" / "000000.cl1", destino / "objetos" / "000001.cl1"
    conteudo_a, conteudo_b = a.read_bytes(), b.read_bytes()
    a.write_bytes(conteudo_b)
    b.write_bytes(conteudo_a)
    with pytest.raises(backup.ErroBackup, match="nao abre"):
        backup.ler_backup(destino, PROVEDOR)


def test_manifesto_adulterado_e_arquivo_a_mais(tmp_path: Path) -> None:
    destino = _gravar(tmp_path)
    caminho = destino / "manifesto.json"
    manifesto = json.loads(caminho.read_text(encoding="utf-8"))
    manifesto["objetos"][0]["sha256"] = "0" * 64
    caminho.write_text(json.dumps(manifesto), encoding="utf-8")
    with pytest.raises(backup.ErroBackup, match="SHA-256 diferente"):
        backup.ler_backup(destino, PROVEDOR)

    destino2 = _gravar(tmp_path, "20261124T130000Z")
    (destino2 / "objetos" / "999999.cl1").write_text("cl1.x", encoding="ascii")
    with pytest.raises(backup.ErroBackup, match="nao bate com o manifesto"):
        backup.ler_backup(destino2, PROVEDOR)


def test_poda_mantem_os_mais_novos(tmp_path: Path) -> None:
    for hora in ("10", "11", "12"):
        _gravar(tmp_path, f"20261124T{hora}0000Z")
    (tmp_path / "incompleto").mkdir()  # sem manifesto: nao e backup, nao e tocado
    removidos = backup.podar(tmp_path, 2)
    assert [p.name for p in removidos] == ["20261124T100000Z"]
    assert [p.name for p in backup.backups(tmp_path)] == ["20261124T110000Z", "20261124T120000Z"]
    assert (tmp_path / "incompleto").is_dir()
    with pytest.raises(backup.ErroBackup):
        backup.podar(tmp_path, 0)


def test_contagens_e_diferencas() -> None:
    contagens = backup.ler_contagens(b"audit_log|10\nevidencia|2\n")
    assert contagens == {"audit_log": 10, "evidencia": 2}
    assert backup.diferencas(contagens, {"audit_log": 10, "evidencia": 3}) == [
        "evidencia: origem 2, copia 3"
    ]


class ComposeFalso:
    """Responde aos comandos do roteiro como a pilha local responderia."""

    def __init__(self, *, integridade_falha: bool = False) -> None:
        self.chamadas: list[list[str]] = []
        self.integridade_falha = integridade_falha

    def __call__(self, argumentos: Sequence[str], entrada: bytes | None) -> bytes:
        chamada = list(argumentos)
        self.chamadas.append(chamada)
        texto = " ".join(chamada)
        if "printf" in texto:
            return b"carbonledger"
        if "psql" in texto:
            sql = (entrada or b"").decode()
            if "alembic_version" in sql:
                return b"0017\n"
            if "information_schema" in sql:
                return b"audit_log|10\nevidencia|2\n"
            return b""
        if "integridade" in chamada:
            if self.integridade_falha:
                raise backup.ErroBackup("docker compose run falhou: 1 tenant com falha")
            return b"1 tenant(s) verificado(s); 0 com falha.\n"
        return b""


def test_ensaio_restaura_confere_e_descarta_a_copia(tmp_path: Path) -> None:
    destino = _gravar(tmp_path)
    compose = ComposeFalso()
    assert backup.ensaio(destino, PROVEDOR, compose, manter_copia=False) == 0
    textos = [" ".join(c) for c in compose.chamadas]
    assert any("pg_restore" in t and "carbonledger_ensaio" in t for t in textos)
    assert any("mc mirror --quiet /entrada local/ensaio-20261124t120000z" in t for t in textos)
    assert any("--banco carbonledger_ensaio --bucket ensaio-20261124t120000z" in t
               for t in textos)  # fmt: skip
    assert "dropdb --if-exists" in textos[-2] and "mc rb --force" in textos[-1]
    # Nenhum segredo na linha de comando: senhas so dentro dos containers.
    assert not any("troque-me" in t or "SECRET" in t for t in textos if "$S3" not in t)


def test_ensaio_com_falha_de_integridade_sai_com_1_e_descarta(tmp_path: Path) -> None:
    destino = _gravar(tmp_path)
    compose = ComposeFalso(integridade_falha=True)
    assert backup.ensaio(destino, PROVEDOR, compose, manter_copia=False) == 1
    assert "mc rb --force" in " ".join(compose.chamadas[-1])


def test_ensaio_com_revisao_diferente_falha(tmp_path: Path) -> None:
    destino = backup.gravar_backup(tmp_path, PROVEDOR, "20261124T140000Z", "0016", b"x", {})
    assert backup.ensaio(destino, PROVEDOR, ComposeFalso(), manter_copia=True) == 1


def test_trocar_banco_so_muda_o_nome() -> None:
    url = "postgresql://app_user:segredo@postgres:5432/carbonledger?ssl=disable"
    assert trocar_banco(url, "carbonledger_ensaio") == (
        "postgresql://app_user:segredo@postgres:5432/carbonledger_ensaio?ssl=disable"
    )
    assert trocar_banco(url, None) == url
    with pytest.raises(ValueError):
        trocar_banco(url, "x; DROP DATABASE y")


class ClienteS3Falso:
    def __init__(self, codigo: str) -> None:
        self.codigo = codigo
        self.pedidos: list[dict[str, Any]] = []

    def get_object(self, **argumentos: Any) -> dict[str, Any]:
        self.pedidos.append(argumentos)
        if "VersionId" in argumentos:
            raise ClientError({"Error": {"Code": self.codigo}}, "GetObject")

        class Corpo:
            def read(self) -> bytes:
                return b"conteudo"

        return {"Body": Corpo()}


def _armazem(cliente: ClienteS3Falso) -> ArmazemS3:
    armazem = ArmazemS3.__new__(ArmazemS3)
    armazem.bucket = "b"
    armazem._cliente = cliente
    return armazem


@pytest.mark.parametrize("codigo", ["NoSuchVersion", "InvalidArgument"])
def test_versao_ausente_le_a_atual(codigo: str) -> None:
    cliente = ClienteS3Falso(codigo)
    assert _armazem(cliente)._ler("k", "v1") == b"conteudo"
    assert [("VersionId" in p) for p in cliente.pedidos] == [True, False]


def test_outro_erro_do_s3_nao_e_engolido() -> None:
    with pytest.raises(ClientError):
        _armazem(ClienteS3Falso("AccessDenied"))._ler("k", "v1")
