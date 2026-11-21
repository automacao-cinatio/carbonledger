"""Telas de organizacao: grupos, empresas e unidades.

Leitura para todos os perfis (o RLS limita o que cada um ve). Grupos e empresas: so
administrador e gestor. Unidades: tambem o responsavel, nas empresas dele (o RLS barra as
demais). Colaborador so le. Acoes por HTMX: sucesso com 204 e HX-Redirect ou HX-Refresh;
erro de campo com 422.
"""

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from app.api.dependencias import SessaoTenant
from app.api.paginas import resposta_com_aviso
from app.core.erros import AcessoNegado
from app.core.security.acesso import Acesso, Perfil, exigir_perfil
from app.modules.organizacao import estrutura
from app.modules.organizacao.validacao import UFS, formatar_cnpj
from app.web.portal import montar_portal, renderizar
from app.web.renderizacao import templates

router = APIRouter(prefix="/organizacao", tags=["organizacao"], include_in_schema=False)

Leitor = Annotated[Acesso, Depends(exigir_perfil(*Perfil))]
Estrutura = Annotated[Acesso, Depends(exigir_perfil(Perfil.ADMINISTRADOR, Perfil.GESTOR))]
EditorUnidades = Annotated[
    Acesso,
    Depends(exigir_perfil(Perfil.ADMINISTRADOR, Perfil.GESTOR, Perfil.RESPONSAVEL)),
]

OPCOES_UF = [(uf, uf) for uf in UFS]

COLUNAS_EMPRESAS = [
    {"chave": "nome", "titulo": "Empresa", "link": "url"},
    {"chave": "cnpj", "titulo": "CNPJ"},
    {"chave": "unidades", "titulo": "Unidades ativas", "unidade": "un.", "casas": 0},
    {"chave": "situacao", "titulo": "Situação", "etiqueta": True},
]
COLUNAS_UNIDADES = [
    {"chave": "nome", "titulo": "Unidade", "link": "url"},
    {"chave": "local", "titulo": "Município"},
    {"chave": "participacao", "titulo": "Participação societária", "unidade": "%", "casas": 2},
    {"chave": "operacional", "titulo": "Controle operacional"},
    {"chave": "financeiro", "titulo": "Controle financeiro"},
    {"chave": "situacao", "titulo": "Situação", "etiqueta": True},
]


@dataclass(frozen=True)
class ProximoPasso:
    texto: str
    url: str | None = None
    rotulo: str = "Continuar"


def _situacao(ativa: bool) -> tuple[str, str]:
    return ("Ativa", "success") if ativa else ("Desativada", "secondary")


def _pode_estrutura(acesso: Acesso) -> bool:
    return acesso.perfil in (Perfil.ADMINISTRADOR, Perfil.GESTOR)


def _pode_unidades(acesso: Acesso) -> bool:
    return acesso.perfil in (Perfil.ADMINISTRADOR, Perfil.GESTOR, Perfil.RESPONSAVEL)


async def _formulario(request: Request) -> dict[str, str]:
    return {chave: str(valor) for chave, valor in (await request.form()).items()}


async def _tela(request: Request, template: str, **contexto: Any) -> HTMLResponse:
    return renderizar(
        request, template, await montar_portal(request), pagina_atual="/organizacao", **contexto
    )


# ---------------------------------------------------------------- indice


@router.get("", response_class=HTMLResponse)
async def indice(request: Request, sessao: SessaoTenant, acesso: Leitor) -> HTMLResponse:
    arvore = await estrutura.listar_arvore(sessao)
    if not acesso.perfil.ve_o_tenant_inteiro:
        arvore = [g for g in arvore if g.empresas]
    grupos = [
        {
            "grupo_id": g.grupo_id,
            "nome": g.nome,
            "linhas": [
                {
                    "nome": e.nome,
                    "url": f"/organizacao/empresas/{e.empresa_id}",
                    "cnpj": formatar_cnpj(e.cnpj) or "Não informado",
                    "unidades": e.unidades_ativas,
                    "situacao": _situacao(e.ativa),
                }
                for e in g.empresas
            ],
        }
        for g in arvore
    ]
    empresas = [e for g in arvore for e in g.empresas if e.ativa]
    sem_unidade = next((e for e in empresas if e.unidades_ativas == 0), None)
    if not arvore:
        proximo = ProximoPasso("Crie o primeiro grupo.", "/organizacao/grupos/novo", "Criar grupo")
    elif not empresas:
        proximo = ProximoPasso(
            "Cadastre a primeira empresa.", "/organizacao/empresas/nova", "Cadastrar empresa"
        )
    elif sem_unidade is not None:
        proximo = ProximoPasso(
            f"Cadastre as unidades de {sem_unidade.nome}.",
            f"/organizacao/empresas/{sem_unidade.empresa_id}",
            "Ver a empresa",
        )
    else:
        proximo = ProximoPasso(
            "A estrutura está completa. O próximo passo é abrir o inventário do ano,"
            " na etapa de coleta."
        )
    return await _tela(
        request,
        "paginas/organizacao/indice.html",
        arvore=grupos,
        colunas_empresas=COLUNAS_EMPRESAS,
        pode_estrutura=_pode_estrutura(acesso),
        proximo=proximo,
    )


# ---------------------------------------------------------------- grupos


@router.get("/grupos/novo", response_class=HTMLResponse)
async def novo_grupo(request: Request, _acesso: Estrutura) -> HTMLResponse:
    return await _tela(
        request,
        "paginas/organizacao/grupo_form.html",
        titulo="Novo grupo",
        acao="/organizacao/grupos",
        grupo=None,
        rascunho="grupo-novo",
    )


@router.post("/grupos")
async def criar_grupo(request: Request, sessao: SessaoTenant, acesso: Estrutura) -> Response:
    formulario = await _formulario(request)
    await estrutura.salvar_grupo(sessao, acesso, formulario.get("nome", ""))
    return resposta_com_aviso("Grupo criado.", **{"HX-Redirect": "/organizacao"})


@router.get("/grupos/{grupo_id}/editar", response_class=HTMLResponse)
async def editar_grupo(
    request: Request, grupo_id: UUID, sessao: SessaoTenant, _acesso: Estrutura
) -> HTMLResponse:
    grupo = await estrutura.obter_grupo(sessao, grupo_id)
    return await _tela(
        request,
        "paginas/organizacao/grupo_form.html",
        titulo=f"Editar o grupo {grupo.nome}",
        acao=f"/organizacao/grupos/{grupo_id}",
        grupo=grupo,
        rascunho=f"grupo-{grupo_id}",
    )


@router.post("/grupos/{grupo_id}")
async def salvar_grupo(
    request: Request, grupo_id: UUID, sessao: SessaoTenant, acesso: Estrutura
) -> Response:
    formulario = await _formulario(request)
    await estrutura.salvar_grupo(sessao, acesso, formulario.get("nome", ""), grupo_id)
    return resposta_com_aviso("Grupo salvo.", **{"HX-Redirect": "/organizacao"})


# ---------------------------------------------------------------- empresas


def _valores_empresa(empresa: estrutura.Empresa | None, grupo_id: str | None) -> dict[str, Any]:
    if empresa is None:
        return {
            "grupo_id": grupo_id,
            "cnpj": "",
            "nome": "",
            "nome_fantasia": "",
            "cnae": "",
            "uf": None,
            "municipio": "",
        }
    return {
        "grupo_id": str(empresa.grupo_id),
        "cnpj": formatar_cnpj(empresa.cnpj),
        "nome": empresa.nome,
        "nome_fantasia": empresa.nome_fantasia or "",
        "cnae": empresa.cnae or "",
        "uf": empresa.uf,
        "municipio": empresa.municipio or "",
    }


async def _form_empresa(
    request: Request,
    sessao: SessaoTenant,
    *,
    titulo: str,
    acao: str,
    voltar: str,
    rascunho: str,
    empresa: estrutura.Empresa | None,
) -> HTMLResponse:
    grupos = [(str(g.grupo_id), g.nome) for g in await estrutura.listar_grupos(sessao)]
    grupo_pedido = request.query_params.get("grupo")
    padrao = grupo_pedido or (grupos[0][0] if len(grupos) == 1 else None)
    return await _tela(
        request,
        "paginas/organizacao/empresa_form.html",
        titulo=titulo,
        acao=acao,
        voltar=voltar,
        rascunho=rascunho,
        grupos=grupos,
        ufs=OPCOES_UF,
        valores=_valores_empresa(empresa, padrao),
    )


@router.get("/empresas/nova", response_class=HTMLResponse)
async def nova_empresa(request: Request, sessao: SessaoTenant, _acesso: Estrutura) -> HTMLResponse:
    return await _form_empresa(
        request,
        sessao,
        titulo="Nova empresa",
        acao="/organizacao/empresas",
        voltar="/organizacao",
        rascunho="empresa-nova",
        empresa=None,
    )


@router.post("/empresas")
async def criar_empresa(request: Request, sessao: SessaoTenant, acesso: Estrutura) -> Response:
    empresa_id = await estrutura.criar_empresa(sessao, acesso, await _formulario(request))
    return resposta_com_aviso(
        "Empresa cadastrada.", **{"HX-Redirect": f"/organizacao/empresas/{empresa_id}"}
    )


@router.get("/empresas/{empresa_id}", response_class=HTMLResponse)
async def ver_empresa(
    request: Request, empresa_id: UUID, sessao: SessaoTenant, acesso: Leitor
) -> HTMLResponse:
    empresa = await estrutura.obter_empresa(sessao, empresa_id)
    grupo = await estrutura.obter_grupo(sessao, empresa.grupo_id)
    unidades = await estrutura.listar_unidades(sessao, empresa_id)
    linhas = [
        {
            "nome": u.nome,
            "url": f"/organizacao/unidades/{u.unidade_id}/editar",
            "local": f"{u.municipio} ({u.uf})",
            "participacao": u.participacao_societaria,
            "operacional": "Sim" if u.controle_operacional else "Não",
            "financeiro": "Sim" if u.controle_financeiro else "Não",
            "situacao": _situacao(u.ativa),
        }
        for u in unidades
    ]
    if not empresa.ativa:
        proximo = ProximoPasso("Reative a empresa para voltar a cadastrar unidades.")
    elif not any(u.ativa for u in unidades):
        proximo = ProximoPasso(
            "Cadastre a primeira unidade desta empresa.",
            f"/organizacao/empresas/{empresa_id}/unidades/nova" if _pode_unidades(acesso) else None,
            "Cadastrar unidade",
        )
    else:
        proximo = ProximoPasso(
            "Confira se todas as unidades estão cadastradas e volte para a organização.",
            "/organizacao",
            "Ir para a organização",
        )
    sede = f"{empresa.municipio} ({empresa.uf})" if empresa.municipio else None
    return await _tela(
        request,
        "paginas/organizacao/empresa.html",
        empresa=empresa,
        grupo=grupo,
        cnpj_formatado=formatar_cnpj(empresa.cnpj) or None,
        sede=sede,
        colunas_unidades=COLUNAS_UNIDADES,
        linhas_unidades=linhas,
        pode_estrutura=_pode_estrutura(acesso),
        pode_unidades=_pode_unidades(acesso),
        proximo=proximo,
    )


@router.get("/empresas/{empresa_id}/editar", response_class=HTMLResponse)
async def editar_empresa(
    request: Request, empresa_id: UUID, sessao: SessaoTenant, _acesso: Estrutura
) -> HTMLResponse:
    empresa = await estrutura.obter_empresa(sessao, empresa_id)
    return await _form_empresa(
        request,
        sessao,
        titulo=f"Editar {empresa.nome}",
        acao=f"/organizacao/empresas/{empresa_id}",
        voltar=f"/organizacao/empresas/{empresa_id}",
        rascunho=f"empresa-{empresa_id}",
        empresa=empresa,
    )


@router.post("/empresas/{empresa_id}")
async def salvar_empresa(
    request: Request, empresa_id: UUID, sessao: SessaoTenant, acesso: Estrutura
) -> Response:
    await estrutura.alterar_empresa(sessao, acesso, empresa_id, await _formulario(request))
    return resposta_com_aviso(
        "Empresa salva.", **{"HX-Redirect": f"/organizacao/empresas/{empresa_id}"}
    )


@router.post("/empresas/{empresa_id}/desativar")
async def desativar_empresa(empresa_id: UUID, sessao: SessaoTenant, acesso: Estrutura) -> Response:
    await estrutura.definir_empresa_ativa(sessao, acesso, empresa_id, ativa=False)
    return resposta_com_aviso("Empresa desativada.", **{"HX-Refresh": "true"})


@router.post("/empresas/{empresa_id}/reativar")
async def reativar_empresa(empresa_id: UUID, sessao: SessaoTenant, acesso: Estrutura) -> Response:
    await estrutura.definir_empresa_ativa(sessao, acesso, empresa_id, ativa=True)
    return resposta_com_aviso("Empresa reativada.", **{"HX-Refresh": "true"})


# ---------------------------------------------------------------- unidades


def _valores_unidade(unidade: estrutura.Unidade | None, uf_padrao: str | None) -> dict[str, Any]:
    if unidade is None:
        return {
            "nome": "",
            "cnpj": "",
            "uf": uf_padrao,
            "municipio": "",
            "participacao_societaria": None,
            "controle_operacional": True,
            "controle_financeiro": True,
        }
    return {
        "nome": unidade.nome,
        "cnpj": formatar_cnpj(unidade.cnpj),
        "uf": unidade.uf,
        "municipio": unidade.municipio,
        "participacao_societaria": unidade.participacao_societaria,
        "controle_operacional": unidade.controle_operacional,
        "controle_financeiro": unidade.controle_financeiro,
    }


@router.get("/empresas/{empresa_id}/unidades/nova", response_class=HTMLResponse)
async def nova_unidade(
    request: Request, empresa_id: UUID, sessao: SessaoTenant, acesso: EditorUnidades
) -> HTMLResponse:
    empresa = await estrutura.obter_empresa(sessao, empresa_id)
    if not empresa.ativa:
        raise AcessoNegado
    return await _tela(
        request,
        "paginas/organizacao/unidade_form.html",
        titulo="Nova unidade",
        acao=f"/organizacao/empresas/{empresa_id}/unidades",
        rascunho=f"unidade-nova-{empresa_id}",
        empresa=empresa,
        unidade=None,
        ufs=OPCOES_UF,
        valores=_valores_unidade(None, empresa.uf),
        pode_unidades=_pode_unidades(acesso),
    )


@router.post("/empresas/{empresa_id}/unidades")
async def criar_unidade(
    request: Request, empresa_id: UUID, sessao: SessaoTenant, acesso: EditorUnidades
) -> Response:
    empresa = await estrutura.obter_empresa(sessao, empresa_id)
    if not empresa.ativa:
        raise AcessoNegado
    await estrutura.criar_unidade(sessao, acesso, empresa_id, await _formulario(request))
    return resposta_com_aviso(
        "Unidade cadastrada.", **{"HX-Redirect": f"/organizacao/empresas/{empresa_id}"}
    )


@router.get("/unidades/{unidade_id}/editar", response_class=HTMLResponse)
async def editar_unidade(
    request: Request, unidade_id: UUID, sessao: SessaoTenant, acesso: Leitor
) -> HTMLResponse:
    # Todos os perfis veem a ficha da unidade; so quem pode editar consegue salvar.
    unidade = await estrutura.obter_unidade(sessao, unidade_id)
    empresa = await estrutura.obter_empresa(sessao, unidade.empresa_id)
    return await _tela(
        request,
        "paginas/organizacao/unidade_form.html",
        titulo=unidade.nome,
        acao=f"/organizacao/unidades/{unidade_id}",
        rascunho=f"unidade-{unidade_id}",
        empresa=empresa,
        unidade=unidade,
        ufs=OPCOES_UF,
        valores=_valores_unidade(unidade, None),
        pode_unidades=_pode_unidades(acesso),
    )


@router.post("/unidades/{unidade_id}")
async def salvar_unidade(
    request: Request, unidade_id: UUID, sessao: SessaoTenant, acesso: EditorUnidades
) -> Response:
    unidade = await estrutura.alterar_unidade(
        sessao, acesso, unidade_id, await _formulario(request)
    )
    return resposta_com_aviso(
        "Unidade salva.", **{"HX-Redirect": f"/organizacao/empresas/{unidade.empresa_id}"}
    )


@router.post("/unidades/{unidade_id}/desativar")
async def desativar_unidade(
    unidade_id: UUID, sessao: SessaoTenant, acesso: EditorUnidades
) -> Response:
    await estrutura.definir_unidade_ativa(sessao, acesso, unidade_id, ativa=False)
    return resposta_com_aviso("Unidade desativada.", **{"HX-Refresh": "true"})


@router.post("/unidades/{unidade_id}/reativar")
async def reativar_unidade(
    unidade_id: UUID, sessao: SessaoTenant, acesso: EditorUnidades
) -> Response:
    await estrutura.definir_unidade_ativa(sessao, acesso, unidade_id, ativa=True)
    return resposta_com_aviso("Unidade reativada.", **{"HX-Refresh": "true"})


# ---------------------------------------------------------------- validacao na hora

VALIDADORES = {
    "cnpj": lambda v: estrutura.validar_cnpj(v, obrigatorio=False)[1],
    "cnae": lambda v: estrutura.validar_cnae(v)[1],
    "participacao_societaria": lambda v: estrutura.validar_participacao(v)[1],
}


@router.post("/validar/{campo}", response_class=HTMLResponse)
async def validar_campo(request: Request, campo: str, _acesso: Leitor) -> HTMLResponse:
    validador = VALIDADORES.get(campo)
    if validador is None:
        raise AcessoNegado
    valor = (await _formulario(request)).get(campo, "")
    return templates.TemplateResponse(
        request, "partes/mensagem_campo.html", {"nome": campo, "erro": validador(valor)}
    )
