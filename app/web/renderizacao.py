"""Templates Jinja2 do portal: filtros pt-BR, URLs de estaticos com versao e menu."""

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import StrictUndefined

from app.web.formatos import formatar_data, formatar_numero

PASTA_WEB = Path(__file__).resolve().parent
PASTA_TEMPLATES = PASTA_WEB / "templates"
PASTA_ESTATICOS = PASTA_WEB / "static"

# Configuracao do htmx: sem estilos injetados (CSP), sem eval e so requisicoes ao proprio app.
CONFIG_HTMX = (
    '{"includeIndicatorStyles": false, "allowEval": false, "allowScriptTags": false,'
    ' "selfRequestsOnly": true, "defaultSwapStyle": "innerHTML"}'
)


@dataclass(frozen=True)
class ItemMenu:
    rotulo: str
    url: str
    icone: str
    disponivel: bool


# Ordem do trabalho: organizar, coletar, calcular, fechar, verificar, relatar, planejar.
# Item entra no menu quando a tela existe (disponivel=True); a ordem nao muda.
MENU: tuple[ItemMenu, ...] = (
    ItemMenu("Início", "/", "home", True),
    ItemMenu("Organização", "/organizacao", "building", True),
    ItemMenu("Coleta", "/coleta", "clipboard-data", False),
    ItemMenu("Resultados", "/resultados", "chart-bar", False),
    ItemMenu("Fechamento", "/fechamento", "lock", False),
    ItemMenu("Verificação", "/verificacao", "shield-check", False),
    ItemMenu("Relatórios", "/relatorios", "file-text", False),
    ItemMenu("Metas", "/metas", "target-arrow", False),
)


@lru_cache(maxsize=256)
def estatico(caminho: str) -> str:
    """URL do arquivo estatico com o hash do conteudo (o navegador nao usa versao velha)."""
    arquivo = (PASTA_ESTATICOS / caminho).resolve()
    if PASTA_ESTATICOS not in arquivo.parents or not arquivo.is_file():
        raise FileNotFoundError(caminho)
    versao = hashlib.sha256(arquivo.read_bytes()).hexdigest()[:10]
    return f"/static/{caminho}?v={versao}"


def criar_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(PASTA_TEMPLATES))
    ambiente = templates.env
    ambiente.undefined = StrictUndefined
    ambiente.trim_blocks = True
    ambiente.lstrip_blocks = True
    ambiente.filters["numero"] = formatar_numero
    ambiente.filters["data"] = formatar_data
    ambiente.globals["estatico"] = estatico
    ambiente.globals["config_htmx"] = CONFIG_HTMX
    ambiente.globals["menu"] = [item for item in MENU if item.disponivel]
    return templates


templates = criar_templates()
