"""Falha se algum arquivo versionado tiver os caracteres U+2013 ou U+2014.

Os caracteres sao escritos por escape para que este arquivo nao se auto-reprove.
Excecao: bibliotecas de terceiros copiadas byte a byte do pacote oficial (pastas vendor),
cuja integridade o MANIFESTO.json trava. Editar esses arquivos quebraria a conferencia.
"""

import subprocess
import sys
from pathlib import Path

PROIBIDOS = {"\u2013": "U+2013", "\u2014": "U+2014"}

# Terceiros, intocaveis: so estas duas pastas, nada alem delas.
PASTAS_DE_TERCEIROS = (("app", "web", "static", "vendor"), ("tests", "e2e", "vendor"))


def de_terceiros(arquivo: Path, raiz: Path) -> bool:
    try:
        partes = arquivo.resolve().relative_to(raiz.resolve()).parts
    except ValueError:
        return False
    return any(partes[: len(pasta)] == pasta for pasta in PASTAS_DE_TERCEIROS)


def listar_arquivos(raiz: Path) -> list[Path]:
    try:
        saida = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],  # noqa: S607
            cwd=raiz,
            capture_output=True,
            check=True,
        ).stdout
        return [raiz / nome for nome in saida.decode("utf-8").split("\0") if nome]
    except (OSError, subprocess.CalledProcessError):
        ignorados = {".git", ".venv", "node_modules", "__pycache__"}
        return [p for p in raiz.rglob("*") if p.is_file() and not ignorados.intersection(p.parts)]


def encontrar_proibidos(arquivos: list[Path]) -> list[str]:
    problemas: list[str] = []
    for arquivo in arquivos:
        if not arquivo.is_file():
            continue
        try:
            texto = arquivo.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binario
        for numero, linha in enumerate(texto.splitlines(), start=1):
            for caractere, nome in PROIBIDOS.items():
                if caractere in linha:
                    problemas.append(f"{arquivo}:{numero}: caractere proibido {nome}")
    return problemas


def main() -> int:
    raiz = Path(__file__).resolve().parent.parent
    arquivos = [a for a in listar_arquivos(raiz) if not de_terceiros(a, raiz)]
    problemas = encontrar_proibidos(arquivos)
    for problema in problemas:
        print(problema)
    if problemas:
        print(f"{len(problemas)} ocorrencia(s) de caracteres proibidos.")
        return 1
    print("Nenhum caractere proibido encontrado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
