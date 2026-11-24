"""Painel consolidado: todas as empresas visiveis, com filtro por grupo.

Todos os perfis leem (o RLS limita as empresas do responsavel e do colaborador).
"""

from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api.dependencias import SessaoTenant
from app.core.security.acesso import Acesso, Perfil, exigir_perfil
from app.modules.relatorios import painel
from app.web.portal import montar_portal, renderizar

router = APIRouter(prefix="/painel", tags=["painel"], include_in_schema=False)

Leitor = Annotated[Acesso, Depends(exigir_perfil(*Perfil))]

COLUNAS_EMPRESAS = [
    {"chave": "empresa", "titulo": "Empresa"},
    {"chave": "grupo", "titulo": "Grupo"},
    {"chave": "inventario", "titulo": "Inventário"},
    {"chave": "situacao", "titulo": "Situação", "etiqueta": True},
    {"chave": "coleta", "titulo": "Dados aprovados", "unidade": "%", "casas": 0},
    {"chave": "escopo1", "titulo": "Escopo 1", "unidade": "tCO₂e", "casas": 3},
    {"chave": "escopo2", "titulo": "Escopo 2", "unidade": "tCO₂e", "casas": 3},
    {"chave": "escopo3", "titulo": "Escopo 3", "unidade": "tCO₂e", "casas": 3},
]
COLUNAS_ANOS = [
    {"chave": "ano", "titulo": "Ano"},
    {"chave": "escopo1", "titulo": "Escopo 1", "unidade": "tCO₂e", "casas": 3},
    {"chave": "escopo2", "titulo": "Escopo 2", "unidade": "tCO₂e", "casas": 3},
    {"chave": "escopo3", "titulo": "Escopo 3", "unidade": "tCO₂e", "casas": 3},
    {"chave": "empresas", "titulo": "Empresas com cálculo", "unidade": "empresas", "casas": 0},
]


def _situacao(linha: painel.LinhaEmpresa) -> tuple[str, str]:
    if linha.ano is None:
        return ("Sem inventário", "secondary")
    if linha.estado == "fechado":
        return ("Fechado", "success")
    if linha.escopo1 is None:
        return ("Em coleta, sem cálculo", "warning")
    return ("Em coleta (preliminar)", "primary")


def _grafico_empresas(linhas: list[painel.LinhaEmpresa]) -> dict[str, Any]:
    # Valores como texto: o Decimal nao vira float no Python; o ECharts le o numero.
    com_numero = [linha for linha in linhas if linha.escopo1 is not None]
    return {
        "legend": {},
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "category", "data": [linha.empresa for linha in com_numero]},
        "yAxis": {"type": "value", "name": "tCO₂e"},
        "series": [
            {
                "type": "bar",
                "stack": "total",
                "name": nome,
                "data": [str(getattr(linha, campo)) for linha in com_numero],
            }
            for nome, campo in (
                ("Escopo 1", "escopo1"),
                ("Escopo 2", "escopo2"),
                ("Escopo 3", "escopo3"),
            )
        ],
    }


def _grafico_anos(serie: list[painel.AnoConsolidado]) -> dict[str, Any]:
    return {
        "legend": {},
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "category", "data": [str(a.ano) for a in serie]},
        "yAxis": {"type": "value", "name": "tCO₂e"},
        "series": [
            {
                "type": "bar",
                "stack": "total",
                "name": nome,
                "data": [str(getattr(a, campo)) for a in serie],
            }
            for nome, campo in (
                ("Escopo 1", "escopo1"),
                ("Escopo 2", "escopo2"),
                ("Escopo 3", "escopo3"),
            )
        ],
    }


@router.get("", response_class=HTMLResponse)
async def indice(
    request: Request, sessao: SessaoTenant, _acesso: Leitor, grupo: str = ""
) -> HTMLResponse:
    opcoes_grupo = await painel.grupos(sessao)
    grupo_id = UUID(grupo) if grupo in {g for g, _ in opcoes_grupo} else None
    linhas, serie = await painel.montar(sessao, grupo_id)
    tabela_empresas = [
        {
            "empresa": linha.empresa,
            "grupo": linha.grupo,
            "inventario": "" if linha.ano is None else str(linha.ano),
            "situacao": _situacao(linha),
            "coleta": None
            if not linha.coleta[1]
            else Decimal(linha.coleta[0] * 100) / Decimal(linha.coleta[1]),
            "escopo1": linha.escopo1,
            "escopo2": linha.escopo2,
            "escopo3": linha.escopo3,
        }
        for linha in linhas
    ]
    tabela_anos = [
        {
            "ano": f"{a.ano} (preliminar)" if a.preliminar else str(a.ano),
            "escopo1": a.escopo1,
            "escopo2": a.escopo2,
            "escopo3": a.escopo3,
            "empresas": a.empresas,
        }
        for a in serie
    ]
    pendentes = [linha.empresa for linha in linhas if linha.estado != "fechado"]
    if not linhas:
        proximo = ("Cadastre as empresas na Organização.", "/organizacao", "Ir para a organização")
    elif pendentes:
        proximo = (
            f"{len(pendentes)} empresa(s) ainda sem inventário fechado. Comece por {pendentes[0]}.",
            "/coleta",
            "Ir para a coleta",
        )
    else:
        proximo = (
            "Todas as empresas com inventário fechado. Acompanhe as metas.",
            "/metas",
            "Ver as metas",
        )
    return renderizar(
        request,
        "paginas/painel/indice.html",
        await montar_portal(request),
        pagina_atual="/painel",
        grupos=[("", "Todos os grupos"), *opcoes_grupo],
        grupo_atual=grupo if grupo_id else "",
        colunas_empresas=COLUNAS_EMPRESAS,
        linhas_empresas=tabela_empresas,
        grafico_empresas=_grafico_empresas(linhas),
        colunas_anos=COLUNAS_ANOS,
        linhas_anos=tabela_anos,
        grafico_anos=_grafico_anos(serie),
        tem_numeros=any(linha.escopo1 is not None for linha in linhas),
        proximo=proximo,
    )
