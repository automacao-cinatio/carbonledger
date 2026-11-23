"""Metas de reducao: lista, criacao, acompanhamento com grafico e acoes do plano.

Leitura para todos os perfis. Criar e arquivar metas: administrador e gestor. Acoes:
administrador, gestor e responsavel (o RLS limita as empresas).
"""

from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from app.api.dependencias import SessaoTenant
from app.api.paginas import resposta_com_aviso
from app.core.security.acesso import Acesso, Perfil, exigir_perfil
from app.modules.metas import servico as metas
from app.modules.organizacao import estrutura
from app.web.portal import montar_portal, renderizar

router = APIRouter(prefix="/metas", tags=["metas"], include_in_schema=False)

Leitor = Annotated[Acesso, Depends(exigir_perfil(*Perfil))]
Gestao = Annotated[Acesso, Depends(exigir_perfil(Perfil.ADMINISTRADOR, Perfil.GESTOR))]
Plano = Annotated[
    Acesso, Depends(exigir_perfil(Perfil.ADMINISTRADOR, Perfil.GESTOR, Perfil.RESPONSAVEL))
]

OPCOES_ESCOPOS = list(metas.ESCOPOS.items())
OPCOES_SITUACAO = list(metas.SITUACOES_ACAO.items())
COLUNAS_METAS = [
    {"chave": "nome", "titulo": "Meta", "link": "url"},
    {"chave": "escopos", "titulo": "Escopos"},
    {"chave": "periodo", "titulo": "Período"},
    {"chave": "reducao", "titulo": "Redução", "unidade": "%", "casas": 1},
    {"chave": "situacao", "titulo": "Situação", "etiqueta": True},
]
COLUNAS_TRAJETORIA = [
    {"chave": "ano", "titulo": "Ano"},
    {"chave": "esperado", "titulo": "Trajetória da meta", "unidade": "tCO₂e", "casas": 3},
    {"chave": "realizado", "titulo": "Realizado", "unidade": "tCO₂e", "casas": 3},
    {"chave": "situacao", "titulo": "Situação", "etiqueta": True},
]
COLUNAS_ACOES = [
    {"chave": "titulo", "titulo": "Ação"},
    {"chave": "ano_inicio", "titulo": "Início"},
    {"chave": "estimativa", "titulo": "Redução estimada", "unidade": "tCO₂e/ano", "casas": 3},
    {"chave": "situacao", "titulo": "Situação", "etiqueta": True},
]
ETIQUETAS_ACAO = {
    "planejada": "secondary",
    "em_andamento": "primary",
    "concluida": "success",
    "cancelada": "secondary",
}


async def _formulario(request: Request) -> dict[str, str]:
    return {chave: str(valor) for chave, valor in (await request.form()).items()}


@router.get("", response_class=HTMLResponse)
async def indice(request: Request, sessao: SessaoTenant, acesso: Leitor) -> HTMLResponse:
    portal = await montar_portal(request)
    contexto: dict[str, Any] = {
        "empresa": None,
        "linhas": [],
        "bases": [],
        "colunas": COLUNAS_METAS,
        "escopos": OPCOES_ESCOPOS,
        "pode_criar": acesso.perfil.ve_o_tenant_inteiro,
    }
    if portal.empresa_atual is None:
        contexto["proximo"] = ("Cadastre a empresa na Organização.", "/organizacao", "Ir")
    else:
        empresa = await estrutura.obter_empresa(sessao, UUID(portal.empresa_atual))
        contexto["empresa"] = empresa
        lista = await metas.listar(sessao, empresa.empresa_id)
        contexto["linhas"] = [
            {
                "nome": m.nome,
                "url": f"/metas/{m.meta_id}",
                "escopos": metas.ESCOPOS[m.escopos],
                "periodo": f"{m.ano_base} a {m.ano_alvo}",
                "reducao": m.reducao_percentual,
                "situacao": ("Ativa", "success") if m.ativa else ("Arquivada", "secondary"),
            }
            for m in lista
        ]
        bases = await metas.inventarios_fechados(sessao, empresa.empresa_id)
        contexto["bases"] = [(str(b.inventario_id), f"Inventário {b.ano}") for b in bases]
        if not bases:
            contexto["proximo"] = (
                "A meta parte de um inventário fechado (o ano-base). Feche o inventário primeiro.",
                "/fechamento",
                "Ir para o fechamento",
            )
        elif not lista:
            contexto["proximo"] = (
                "Defina a primeira meta: escopos, ano-alvo e a redução que a empresa assume.",
                None,
                "",
            )
        else:
            contexto["proximo"] = (
                "Acompanhe cada meta e cadastre as ações do plano de descarbonização.",
                f"/metas/{lista[0].meta_id}",
                "Ver a meta",
            )
    return renderizar(
        request, "paginas/metas/indice.html", portal, pagina_atual="/metas", **contexto
    )


@router.post("/empresas/{empresa_id}")
async def criar(
    request: Request, empresa_id: UUID, sessao: SessaoTenant, acesso: Gestao
) -> Response:
    await estrutura.obter_empresa(sessao, empresa_id)
    meta_id = await metas.criar(sessao, acesso, empresa_id, await _formulario(request))
    return resposta_com_aviso("Meta criada.", **{"HX-Redirect": f"/metas/{meta_id}"})


def _grafico(pontos: list[metas.Ponto]) -> dict[str, Any]:
    # Valores como texto: o Decimal nao vira float no Python; o ECharts le o numero.
    return {
        "legend": {},
        "xAxis": {"type": "category", "data": [str(p.ano) for p in pontos]},
        "yAxis": {"type": "value", "name": "tCO₂e"},
        "series": [
            {
                "type": "line",
                "name": "Trajetória da meta",
                "data": [str(p.esperado) for p in pontos],
            },
            {
                "type": "line",
                "name": "Realizado",
                "data": [None if p.realizado is None else str(p.realizado) for p in pontos],
            },
        ],
    }


@router.get("/{meta_id}", response_class=HTMLResponse)
async def ver(
    request: Request, meta_id: UUID, sessao: SessaoTenant, acesso: Leitor
) -> HTMLResponse:
    meta = await metas.obter(sessao, meta_id)
    empresa = await estrutura.obter_empresa(sessao, meta.empresa_id)
    pontos = await metas.acompanhamento(sessao, meta)
    acoes = await metas.listar_acoes(sessao, meta_id)
    linhas = [
        {
            "ano": str(p.ano) + (" (preliminar)" if p.preliminar else ""),
            "esperado": p.esperado,
            "realizado": p.realizado,
            "situacao": ("Sem inventário", "secondary")
            if p.no_caminho is None
            else ("No caminho", "success")
            if p.no_caminho
            else ("Acima da trajetória", "danger"),
        }
        for p in pontos
    ]
    ativas = [a for a in acoes if a.situacao != "cancelada"]
    estimado = sum((a.reducao_estimada_tco2e for a in ativas), Decimal(0))
    lacuna = meta.valor_base_tco2e - meta.valor_alvo_tco2e
    ultimo = next((p for p in reversed(pontos) if p.realizado is not None), None)
    proximo: tuple[str, str | None, str]
    if not ativas:
        proximo = ("Cadastre as ações que vão levar a empresa até a meta.", None, "")
    elif estimado < lacuna:
        proximo = (
            "As ações cadastradas não cobrem a redução da meta. Planeje mais ações ou revise as"
            " estimativas.",
            None,
            "",
        )
    elif ultimo is not None and ultimo.no_caminho is False:
        proximo = (
            f"Em {ultimo.ano} as emissões ficaram acima da trajetória."
            " Reveja o andamento das ações.",
            None,
            "",
        )
    else:
        proximo = (
            "O plano cobre a meta. Atualize a situação das ações e feche o inventário de cada ano"
            " para acompanhar.",
            "/coleta",
            "Ir para a coleta",
        )
    return renderizar(
        request,
        "paginas/metas/meta.html",
        await montar_portal(request),
        pagina_atual="/metas",
        meta=meta,
        empresa=empresa,
        escopos=metas.ESCOPOS[meta.escopos],
        colunas_trajetoria=COLUNAS_TRAJETORIA,
        linhas_trajetoria=linhas,
        dados_grafico=_grafico(pontos),
        colunas_acoes=COLUNAS_ACOES,
        linhas_acoes=[
            {
                "titulo": a.titulo,
                "ano_inicio": str(a.ano_inicio),
                "estimativa": a.reducao_estimada_tco2e,
                "situacao": (metas.SITUACOES_ACAO[a.situacao], ETIQUETAS_ACAO[a.situacao]),
            }
            for a in acoes
        ],
        acoes=acoes,
        situacoes=OPCOES_SITUACAO,
        estimado=estimado,
        lacuna=lacuna,
        pode_metas=acesso.perfil.ve_o_tenant_inteiro,
        pode_acoes=acesso.perfil in (Perfil.ADMINISTRADOR, Perfil.GESTOR, Perfil.RESPONSAVEL),
        proximo=proximo,
    )


@router.post("/{meta_id}/arquivar")
async def arquivar(meta_id: UUID, sessao: SessaoTenant, acesso: Gestao) -> Response:
    await metas.definir_ativa(sessao, acesso, meta_id, ativa=False)
    return resposta_com_aviso("Meta arquivada.", **{"HX-Refresh": "true"})


@router.post("/{meta_id}/reativar")
async def reativar(meta_id: UUID, sessao: SessaoTenant, acesso: Gestao) -> Response:
    await metas.definir_ativa(sessao, acesso, meta_id, ativa=True)
    return resposta_com_aviso("Meta reativada.", **{"HX-Refresh": "true"})


@router.post("/{meta_id}/acoes")
async def criar_acao(
    request: Request, meta_id: UUID, sessao: SessaoTenant, acesso: Plano
) -> Response:
    await metas.criar_acao(sessao, acesso, meta_id, await _formulario(request))
    return resposta_com_aviso("Ação cadastrada.", **{"HX-Refresh": "true"})


@router.post("/acoes/{acao_id}/situacao")
async def mudar_situacao(
    request: Request, acao_id: UUID, sessao: SessaoTenant, acesso: Plano
) -> Response:
    # Cada acao tem o proprio campo (situacao_1, situacao_2...): ids unicos na tela.
    formulario = await _formulario(request)
    situacao = next((v for k, v in formulario.items() if k.startswith("situacao")), "")
    await metas.mudar_situacao(sessao, acesso, acao_id, situacao)
    return resposta_com_aviso("Situação da ação atualizada.", **{"HX-Refresh": "true"})
