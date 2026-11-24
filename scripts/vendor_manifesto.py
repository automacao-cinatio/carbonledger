"""Gera (ou confere, com --conferir) o manifesto de hashes de app/web/static/vendor.

Bibliotecas de terceiros entram com versao fixa no nome da pasta, baixadas do registro npm
com a integridade (sha512) conferida. O manifesto fixa o SHA-256 de cada arquivo: qualquer
mudanca em vendor/ sem atualizar o manifesto quebra o teste.
"""

import hashlib
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
VENDOR = RAIZ / "app" / "web" / "static" / "vendor"
MANIFESTO = VENDOR / "MANIFESTO.json"

PACOTES = {
    "tabler-1.7.5": {"npm": "@tabler/core@1.7.5", "licenca": "MIT"},
    "tabler-icons-3.52.0": {"npm": "@tabler/icons-webfont@3.52.0", "licenca": "MIT"},
    "htmx-2.0.11": {"npm": "htmx.org@2.0.11", "licenca": "0BSD"},
    "echarts-6.1.0": {"npm": "echarts@6.1.0", "licenca": "Apache-2.0"},
}


def calcular() -> dict[str, object]:
    arquivos = {
        caminho.relative_to(VENDOR).as_posix(): hashlib.sha256(caminho.read_bytes()).hexdigest()
        for caminho in sorted(VENDOR.rglob("*"))
        if caminho.is_file() and caminho != MANIFESTO
    }
    return {"pacotes": PACOTES, "arquivos": arquivos}


def main() -> int:
    atual = calcular()
    if "--conferir" in sys.argv:
        gravado = json.loads(MANIFESTO.read_text(encoding="utf-8"))
        if gravado != atual:
            print("vendor/ diferente do MANIFESTO.json")
            return 1
        print("vendor/ confere com o MANIFESTO.json")
        return 0
    MANIFESTO.write_text(json.dumps(atual, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(atual['arquivos'])} arquivos no manifesto")  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    sys.exit(main())
