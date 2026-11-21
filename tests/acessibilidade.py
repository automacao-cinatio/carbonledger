"""Pecas comuns dos testes de navegador: axe-core, erros de console e servidor local."""

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI

AXE = Path(__file__).parent / "e2e" / "vendor" / "axe-core-4.14.1" / "axe.min.js"
REGRAS_WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def servir(app: FastAPI, porta: int) -> Iterator[str]:
    """Sobe o app num servidor uvicorn de verdade, numa thread, ate o fim do teste."""
    servidor = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=porta, log_level="warning")
    )
    linha = threading.Thread(target=servidor.run, daemon=True)
    linha.start()
    limite = time.monotonic() + 30
    while not servidor.started:
        if time.monotonic() > limite:
            raise RuntimeError("servidor de teste nao subiu")
        time.sleep(0.1)
    yield f"http://127.0.0.1:{porta}"
    servidor.should_exit = True
    linha.join(timeout=10)


def vigiar_console(pagina: Any) -> list[str]:
    erros: list[str] = []
    pagina.on("console", lambda m: m.type in ("error", "warning") and erros.append(m.text))
    pagina.on("pageerror", lambda e: erros.append(str(e)))
    return erros


def violacoes_axe(pagina: Any) -> list[dict[str, Any]]:
    # Pelo protocolo do DevTools: a CSP da pagina (corretamente) barra script injetado.
    cdp = pagina.context.new_cdp_session(pagina)
    cdp.send("Runtime.evaluate", {"expression": AXE.read_text(encoding="utf-8")})
    resultado = pagina.evaluate(
        "regras => axe.run(document, {runOnly: {type: 'tag', values: regras}})", REGRAS_WCAG
    )
    return [
        {"id": v["id"], "alvos": [n["target"] for n in v["nodes"]][:3], "ajuda": v["help"]}
        for v in resultado["violations"]
    ]
