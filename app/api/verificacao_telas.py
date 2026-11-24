"""Verificacao: concessoes (administrador) e o painel so de leitura do verificador.

Do lado do cliente: todos os perfis veem as concessoes da empresa em foco; so o
administrador concede e revoga. Do lado do verificador: so as rotas /verificacao/painel,
/verificacao/fontes, evidencias e memoria, com a sessao no papel app_verificador, que o banco
limita ao inventario da concessao (migracao 0010). Qualquer outra tela responde 404 para ele.
"""

from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from app.api.dependencias import SessaoTenant, conexao_asyncpg
from app.api.paginas import resposta_com_aviso
from app.api.resultados_telas import resposta_da_memoria
from app.core import email as servico_email
from app.core.config import Configuracao, obter_configuracao
from app.core.erros import AcessoNegado
from app.core.security.acesso import (
    Acesso,
    Perfil,
    Verificacao,
    exigir_perfil,
    exigir_verificacao,
    obter_sessao_usuario,
    resolver_verificacao,
)
from app.core.security.sessao import ArmazemSessoes, Sessao
from app.modules.coleta import evidencias
from app.modules.coleta import servico as coleta
from app.modules.coleta.categorias import periodos, rotulo, rotulo_periodo
from app.modules.fechamento import servico as fechamento
from app.modules.organizacao import estrutura
from app.modules.relatorios import relatorio
from app.modules.verificacao import servico as verificacao
from app.web.portal import montar_portal, renderizar

router = APIRouter(prefix="/verificacao", tags=["verificacao"], include_in_schema=False)

FUSO = ZoneInfo("America/Sao_Paulo")

Leitor = Annotated[Acesso, Depends(exigir_perfil(*Perfil))]
Administrador = Annotated[Acesso, Depends(exigir_perfil(Perfil.ADMINISTRADOR))]
Verificador = Annotated[Verificacao, Depends(exigir_verificacao)]
SessaoAtual = Annotated[Sessao, Depends(obter_sessao_usuario)]
Config = Annotated[Configuracao, Depends(obter_configuracao)]

ETIQUETAS = {
    "ativa": ("Ativa", "success"),
    "aguardando aceite": ("Aguardando aceite", "warning"),
    "vencida": ("Vencida", "secondary"),
    "revogada": ("Revogada", "secondary"),
}
SITUACOES = {
    None: ("Sem dado", "secondary"),
    "aguardando": ("Aguardando aprovação", "warning"),
    "aprovado": ("Aprovado", "success"),
    "devolvido": ("Devolvido", "danger"),
}


def _hora(valor: Any) -> str:
    return valor.astimezone(FUSO).strftime("%d/%m/%Y %H:%M")


async def _formulario(request: Request) -> dict[str, str]:
    return {chave: str(valor) for chave, valor in (await request.form()).items()}


# ---------------------------------------------------------------- lado do cliente


@router.get("", response_class=HTMLResponse)
async def indice(request: Request, sessao: SessaoTenant, acesso: Leitor) -> HTMLResponse:
    portal = await montar_portal(request)
    contexto: dict[str, Any] = {
        "empresa": None,
        "concessoes": [],
        "inventarios": [],
        "pode_conceder": acesso.perfil is Perfil.ADMINISTRADOR,
        "prazo_padrao": verificacao.PRAZO_PADRAO_DIAS,
    }
    if portal.empresa_atual is None:
        contexto["proximo"] = ("Cadastre a empresa na Organização.", "/organizacao", "Ir")
    else:
        empresa = await estrutura.obter_empresa(sessao, UUID(portal.empresa_atual))
        contexto["empresa"] = empresa
        contexto["concessoes"] = [
            {
                "concessao_id": c.concessao_id,
                "titulo": f"Inventário {c.ano}: {c.email}",
                "etiqueta": ETIQUETAS[c.situacao],
                "itens": [
                    ("Concedida em", _hora(c.criado_em)),
                    ("Vale até", _hora(c.expira_em)),
                    ("Aceita em", _hora(c.aceito_em) if c.aceito_em else None),
                ],
                "revogavel": c.situacao in ("ativa", "aguardando aceite"),
            }
            for c in await verificacao.listar(sessao, empresa.empresa_id)
        ]
        fechados = [
            (str(i.inventario_id), f"Inventário {i.ano}")
            for i in await coleta.listar_inventarios(sessao, empresa.empresa_id)
            if i.estado == "fechado"
        ]
        contexto["inventarios"] = fechados
        if not fechados:
            contexto["proximo"] = (
                "Feche o inventário antes de dar acesso ao verificador.",
                "/fechamento",
                "Ir para o fechamento",
            )
        else:
            contexto["proximo"] = (
                "Conceda o acesso ao verificador pelo e-mail dele. Ele recebe um link de uso"
                " único e vê só o inventário escolhido, até o prazo.",
                None,
                "",
            )
    return renderizar(
        request, "paginas/verificacao/indice.html", portal, pagina_atual="/verificacao", **contexto
    )


@router.post("/concessoes")
async def conceder(
    request: Request, sessao: SessaoTenant, acesso: Administrador, configuracao: Config
) -> Response:
    token, email, ano, prazo = await verificacao.conceder(
        sessao, acesso, await _formulario(request)
    )
    dados = (
        await sessao.execute(
            text(
                "SELECT t.nome AS tenant, e.nome AS empresa FROM concessao_verificacao c"
                " JOIN tenant t ON t.tenant_id = c.tenant_id"
                " JOIN empresa e ON e.tenant_id = c.tenant_id AND e.empresa_id = c.empresa_id"
                " WHERE c.concessao_id = :id"
            ),
            {"id": token.convite_id},
        )
    ).one()
    base = configuracao.app_url_base.rstrip("/")
    links = {
        nome: f"{base}/verificacao/convite/{token.codificar()}/{nome}"
        for nome in request.app.state.provedores
    }
    assunto, corpo = verificacao.texto_da_concessao(dados.tenant, dados.empresa, ano, links, prazo)
    # Se o e-mail falhar, a excecao desfaz a concessao (a sessao faz rollback).
    await servico_email.enviar_email(configuracao, email, assunto, corpo)
    return resposta_com_aviso(
        "Acesso concedido. O verificador recebeu o link por e-mail.", **{"HX-Refresh": "true"}
    )


@router.post("/concessoes/{concessao_id}/revogar")
async def revogar(concessao_id: UUID, sessao: SessaoTenant, acesso: Administrador) -> Response:
    await verificacao.revogar(sessao, acesso, concessao_id)
    return resposta_com_aviso(
        "Acesso revogado. O verificador perde o acesso na próxima tela que abrir.",
        **{"HX-Refresh": "true"},
    )


# ---------------------------------------------------------------- lado do verificador


@router.post("/abrir/{tenant_id}/{concessao_id}")
async def abrir(
    request: Request, tenant_id: UUID, concessao_id: UUID, sessao: SessaoAtual
) -> Response:
    achada = await resolver_verificacao(
        request.app.state.fabrica_sessoes, sessao.usuario_id, tenant_id, concessao_id
    )
    if achada is None:
        raise AcessoNegado
    armazem: ArmazemSessoes = request.app.state.sessoes
    await armazem.abrir_verificacao(sessao, tenant_id, concessao_id)
    return resposta_com_aviso("Verificação aberta.", **{"HX-Redirect": "/verificacao/painel"})


@router.get("/painel", response_class=HTMLResponse)
async def painel(request: Request, sessao: SessaoTenant, atual: Verificador) -> HTMLResponse:
    inventario = await coleta.obter_inventario(sessao, atual.inventario_id)
    conexao = await conexao_asyncpg(sessao)
    situacao = await fechamento.situacao(conexao, atual.inventario_id)
    hash_atual = situacao.fechamento_atual.hash_fechamento if situacao.fechamento_atual else None
    relatorios = [
        {
            "gerado_em": _hora(r.gerado_em),
            "url": f"/verificacao/relatorios/{r.relatorio_id}/arquivo.pdf",
            "versao": r.versao_fechamento,
            "assinante": r.titular,
            "hash": r.sha256.hex(),
            "situacao": ("Fechamento atual", "success")
            if r.hash_fechamento == hash_atual
            else ("Fechamento anterior", "secondary"),
        }
        for r in await relatorio.listar(conexao, atual.inventario_id)
    ]
    calculo_final = situacao.fechamento_atual.calculo_id if situacao.fechamento_atual else None
    calculo = None
    if calculo_final is not None:
        calculo = (
            await sessao.execute(
                text(
                    "SELECT calculo_id, metodo, metodo_versao, escopo1_tco2e, escopo2_tco2e,"
                    " escopo3_tco2e, biogenico_tco2, hash_resultado, conhecido_em FROM calculo"
                    " WHERE calculo_id = :id"
                ),
                {"id": calculo_final},
            )
        ).first()
    situacoes = await coleta.situacao_das_fontes(sessao, atual.inventario_id)
    fontes = [
        {
            "descricao": s.fonte.descricao,
            "url": f"/verificacao/fontes/{s.fonte.fonte_id}",
            "unidade": s.fonte.unidade_nome,
            "categoria": rotulo(s.fonte.categoria),
            "aprovados": s.progresso.aprovados,
            "esperados": s.progresso.esperados,
            "com_evidencia": s.progresso.com_evidencia,
        }
        for s in situacoes
        if s.fonte.conta_no_progresso
    ]
    excluidas = [
        {"descricao": s.fonte.descricao, "justificativa": s.fonte.justificativa or ""}
        for s in situacoes
        if s.fonte.ativa and not s.fonte.material
    ]
    return renderizar(
        request,
        "paginas/verificacao/painel.html",
        await montar_portal(request),
        verificacao=atual,
        inventario=inventario,
        situacao=situacao,
        calculo=calculo,
        conhecido_em=_hora(calculo.conhecido_em) if calculo else None,
        fontes=fontes,
        excluidas=excluidas,
        relatorios=relatorios,
    )


@router.get("/fontes/{fonte_id}", response_class=HTMLResponse)
async def ver_fonte(
    request: Request, fonte_id: UUID, sessao: SessaoTenant, atual: Verificador
) -> HTMLResponse:
    fonte = await coleta.obter_fonte(sessao, fonte_id)
    situacao = (await coleta.situacao_das_fontes(sessao, fonte.inventario_id, fonte_id))[0]
    linhas = []
    for periodo in periodos(fonte.periodicidade):
        dado = situacao.atuais.get(periodo)
        linhas.append(
            {
                "periodo": rotulo_periodo(periodo),
                "quantidade": dado.quantidade if dado else None,
                "data_compra": dado.data_compra.strftime("%d/%m/%Y")
                if dado and dado.data_compra
                else "",
                "versao": dado.versao if dado else None,
                "situacao": SITUACOES[dado.situacao if dado else None],
                "evidencias": situacao.evidencias_do_periodo(periodo),
            }
        )
    lista = await evidencias.listar_evidencias(sessao, fonte_id)
    return renderizar(
        request,
        "paginas/verificacao/fonte.html",
        await montar_portal(request),
        verificacao=atual,
        fonte=fonte,
        categoria=rotulo(fonte.categoria),
        colunas=[
            {"chave": "periodo", "titulo": "Período"},
            {
                "chave": "quantidade",
                "titulo": "Quantidade",
                "unidade": fonte.unidade_medida,
                "exato": True,
            },
            {"chave": "data_compra", "titulo": "Data da compra"},
            {"chave": "versao", "titulo": "Versão", "unidade": "nº", "casas": 0},
            {"chave": "situacao", "titulo": "Situação", "etiqueta": True},
            {"chave": "evidencias", "titulo": "Evidências", "unidade": "arquivos", "casas": 0},
        ],
        linhas=linhas,
        colunas_evidencias=[
            {"chave": "nome", "titulo": "Arquivo", "link": "url"},
            {"chave": "periodo", "titulo": "Período"},
            {"chave": "enviado_em", "titulo": "Enviado em"},
            {"chave": "hash", "titulo": "SHA-256"},
        ],
        linhas_evidencias=[
            {
                "nome": e.nome_arquivo,
                "url": f"/verificacao/evidencias/{e.evidencia_id}/arquivo",
                "periodo": "Todos" if e.periodo is None else rotulo_periodo(e.periodo),
                "enviado_em": _hora(e.enviado_em),
                "hash": e.sha256_hex,
            }
            for e in lista
        ],
    )


@router.get("/evidencias/{evidencia_id}/arquivo")
async def baixar_evidencia(
    request: Request, evidencia_id: UUID, sessao: SessaoTenant, _atual: Verificador
) -> Response:
    evidencia, conteudo = await evidencias.ler_evidencia(
        sessao, evidencia_id, request.app.state.armazem
    )
    return Response(
        content=conteudo,
        media_type=evidencia.tipo_conteudo,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(evidencia.nome_arquivo)}",
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/relatorios/{relatorio_id}/arquivo.pdf")
async def baixar_relatorio(
    request: Request, relatorio_id: UUID, sessao: SessaoTenant, _atual: Verificador
) -> Response:
    dados, ano, conteudo = await relatorio.ler(
        await conexao_asyncpg(sessao), relatorio_id, request.app.state.armazem
    )
    nome = f"inventario-{ano}-fechamento-v{dados.versao_fechamento}.pdf"
    return Response(
        content=conteudo,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(nome)}",
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/calculos/{calculo_id}/memoria.csv")
async def memoria(calculo_id: UUID, sessao: SessaoTenant, _atual: Verificador) -> Response:
    return await resposta_da_memoria(sessao, calculo_id)
