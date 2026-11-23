"""Telas de resultados: calcular os escopos 1 e 2, ver totais, pendencias, historico e memoria.

Leitura para todos os perfis (o RLS limita as empresas). Calcular e conferir: administrador,
gestor e responsavel. O calculo roda na propria requisicao, com a conexao asyncpg da sessao
(contexto de tenant ja aplicado), pela mesma funcao que o worker usa.

A plataforma calcula e organiza; quem atesta e o verificador. A tela diz isso.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from app.api.dependencias import SessaoTenant, conexao_asyncpg
from app.api.paginas import resposta_com_aviso
from app.core.csv_seguro import gerar_csv
from app.core.erros import AcessoNegado
from app.core.security.acesso import Acesso, Perfil, exigir_perfil
from app.modules.calculo import servico as calculo
from app.modules.coleta import servico as coleta
from app.modules.coleta.categorias import rotulo, rotulo_periodo
from app.modules.organizacao import estrutura
from app.web.formatos import formatar_numero
from app.web.portal import montar_portal, renderizar

router = APIRouter(prefix="/resultados", tags=["resultados"], include_in_schema=False)

FUSO = ZoneInfo("America/Sao_Paulo")

Leitor = Annotated[Acesso, Depends(exigir_perfil(*Perfil))]
Revisor = Annotated[
    Acesso,
    Depends(exigir_perfil(Perfil.ADMINISTRADOR, Perfil.GESTOR, Perfil.RESPONSAVEL)),
]

MOTIVOS = {
    "falta_dado": ("Dado não aprovado", "Lance e aprove o dado do período, na coleta."),
    "sem_atividade": (
        "Fonte sem combustível ou atividade",
        "Edite a fonte e escolha o combustível ou a atividade.",
    ),
    "sem_fator": (
        "Sem fator aprovado",
        "A curadoria precisa aprovar o fator para essa atividade e data.",
    ),
    "unidade_incompativel": (
        "Unidade sem conversão",
        "Lance o dado na mesma grandeza do fator: o método não converte volume em massa.",
    ),
    "sem_gwp": (
        "Gás sem GWP",
        "A curadoria precisa aprovar o GWP do gás no conjunto do inventário.",
    ),
}

COLUNAS_CATEGORIA = [
    {"chave": "nome", "titulo": "Categoria"},
    {"chave": "valor", "titulo": "Emissões", "unidade": "tCO₂e", "casas": 3},
]
COLUNAS_GAS = [
    {"chave": "nome", "titulo": "Gás"},
    {"chave": "valor", "titulo": "Emissões", "unidade": "tCO₂e", "casas": 3},
]
COLUNAS_PENDENCIAS = [
    {"chave": "fonte", "titulo": "Fonte", "link": "url"},
    {"chave": "periodo", "titulo": "Período"},
    {"chave": "motivo", "titulo": "Motivo", "etiqueta": True},
    {"chave": "detalhe", "titulo": "Detalhe"},
    {"chave": "acao", "titulo": "Como resolver"},
]
COLUNAS_HISTORICO = [
    {"chave": "quando", "titulo": "Calculado em", "link": "url"},
    {"chave": "total", "titulo": "Escopo 1", "unidade": "tCO₂e", "casas": 3},
    {"chave": "escopo2", "titulo": "Escopo 2", "unidade": "tCO₂e", "casas": 3},
    {"chave": "escopo3", "titulo": "Escopo 3", "unidade": "tCO₂e", "casas": 3},
    {"chave": "pendencias", "titulo": "Pendências", "unidade": "itens", "casas": 0},
    {"chave": "hash", "titulo": "Hash do resultado (início)"},
]
CABECALHO_MEMORIA = (
    "linha", "fonte", "periodo", "categoria", "atividade", "componente", "percentual_mistura",
    "data_referencia", "quantidade", "unidade", "quantidade_convertida", "gas", "biogenico",
    "fator", "unidade_fator", "fator_versao_id", "gwp", "gwp_versao_id",
    "percentual_consolidado", "massa_t", "tco2e",
)  # fmt: skip


def _hora(valor: datetime) -> str:
    return valor.astimezone(FUSO).strftime("%d/%m/%Y %H:%M")


def _decimal_csv(valor: Decimal | None) -> str:
    """Sem arredondar e sem milhar: a memoria precisa reproduzir a conta."""
    if valor is None:
        return ""
    normalizado = valor.normalize()
    return (format(normalizado, "f") if normalizado != 0 else "0").replace(".", ",")


async def _calculos(sessao: SessaoTenant, inventario_id: UUID) -> list[Any]:
    linhas = await sessao.execute(
        text(
            "SELECT calculo_id, metodo, metodo_versao, gwp_conjunto, conhecido_em, hash_entrada,"
            " hash_resultado, escopo1_tco2e, escopo2_tco2e, escopo3_tco2e, biogenico_tco2, linhas,"
            " pendencias,"
            " calculado_em"
            " FROM calculo WHERE inventario_id = :id ORDER BY calculado_em DESC"
        ),
        {"id": inventario_id},
    )
    return list(linhas)


async def _linhas_do_calculo(sessao: SessaoTenant, calculo_id: UUID) -> list[Any]:
    return list(
        await sessao.execute(
            text(
                "SELECT l.*, f.descricao AS fonte FROM calculo_linha l"
                " JOIN fonte_emissao f ON f.tenant_id = l.tenant_id AND f.fonte_id = l.fonte_id"
                " WHERE l.calculo_id = :id ORDER BY l.linha"
            ),
            {"id": calculo_id},
        )
    )


async def _pendencias(sessao: SessaoTenant, calculo_id: UUID) -> list[dict[str, Any]]:
    linhas = await sessao.execute(
        text(
            "SELECT p.fonte_id, p.periodo, p.motivo, p.detalhe, f.descricao AS fonte"
            " FROM calculo_pendencia p"
            " JOIN fonte_emissao f ON f.tenant_id = p.tenant_id AND f.fonte_id = p.fonte_id"
            " WHERE p.calculo_id = :id ORDER BY p.item"
        ),
        {"id": calculo_id},
    )
    return [
        {
            "fonte": p.fonte,
            "url": f"/coleta/fontes/{p.fonte_id}"
            if p.periodo is None
            else f"/coleta/fontes/{p.fonte_id}/periodos/{p.periodo}",
            "periodo": "Todos" if p.periodo is None else rotulo_periodo(p.periodo),
            "motivo": (MOTIVOS[p.motivo][0], "warning"),
            "detalhe": p.detalhe,
            "acao": MOTIVOS[p.motivo][1],
        }
        for p in linhas
    ]


def _somar(linhas: list[Any], chave: str) -> list[dict[str, Any]]:
    totais: dict[str, Decimal] = {}
    for linha in linhas:
        if linha.tco2e is None:
            continue
        nome = getattr(linha, chave)
        totais[nome] = totais.get(nome, Decimal(0)) + linha.tco2e
    return [
        {"nome": rotulo(nome) if chave == "categoria" else nome, "valor": valor}
        for nome, valor in sorted(totais.items(), key=lambda par: -par[1])
    ]


def _grafico(por_categoria: list[dict[str, Any]]) -> dict[str, Any]:
    # Valores como texto: o Decimal nao vira float no Python; o ECharts le o numero.
    return {
        "xAxis": {"type": "category", "data": [c["nome"] for c in por_categoria]},
        "yAxis": {"type": "value", "name": "tCO₂e"},
        "series": [
            {"type": "bar", "name": "Emissões", "data": [str(c["valor"]) for c in por_categoria]}
        ],
    }


async def _tela_do_calculo(
    request: Request,
    sessao: SessaoTenant,
    acesso: Acesso,
    inventario: coleta.Inventario | None,
    calculo_id: UUID | None,
) -> HTMLResponse:
    portal = await montar_portal(request)
    contexto: dict[str, Any] = {
        "inventario": inventario,
        "empresa": None,
        "atual": None,
        "historico": [],
        "pode_calcular": False,
        "colunas_categoria": COLUNAS_CATEGORIA,
        "colunas_gas": COLUNAS_GAS,
        "colunas_pendencias": COLUNAS_PENDENCIAS,
        "colunas_historico": COLUNAS_HISTORICO,
    }
    if inventario is None:
        contexto["proximo"] = {
            "texto": "Abra o inventário e colete os dados antes de calcular.",
            "url": "/coleta",
            "rotulo": "Ir para a coleta",
        }
        return renderizar(
            request,
            "paginas/resultados/indice.html",
            portal,
            pagina_atual="/resultados",
            **contexto,
        )
    contexto["empresa"] = await estrutura.obter_empresa(sessao, inventario.empresa_id)
    calculos = await _calculos(sessao, inventario.inventario_id)
    contexto["pode_calcular"] = (
        acesso.perfil in (Perfil.ADMINISTRADOR, Perfil.GESTOR, Perfil.RESPONSAVEL)
        and inventario.em_coleta
    )
    contexto["historico"] = [
        {
            "quando": _hora(c.calculado_em),
            "url": f"/resultados/calculos/{c.calculo_id}",
            "total": c.escopo1_tco2e,
            "escopo2": c.escopo2_tco2e,
            "escopo3": c.escopo3_tco2e,
            "pendencias": c.pendencias,
            "hash": bytes(c.hash_resultado).hex()[:12],
        }
        for c in calculos
    ]
    escolhido = next(
        (c for c in calculos if calculo_id is None or c.calculo_id == calculo_id), None
    )
    if calculo_id is not None and escolhido is None:
        raise AcessoNegado
    if escolhido is None:
        contexto["proximo"] = {
            "texto": "Calcule as emissões com os dados aprovados. O que faltar aparece como"
            " pendência, nunca como zero.",
            "url": None,
            "rotulo": "",
        }
    else:
        linhas = await _linhas_do_calculo(sessao, escolhido.calculo_id)
        pendencias = await _pendencias(sessao, escolhido.calculo_id)
        por_categoria = _somar(linhas, "categoria")
        contexto["atual"] = {
            "calculo_id": escolhido.calculo_id,
            "mais_recente": escolhido.calculo_id == calculos[0].calculo_id,
            "escopo1": escolhido.escopo1_tco2e,
            "escopo2": escolhido.escopo2_tco2e,
            "escopo3": escolhido.escopo3_tco2e,
            "biogenico": escolhido.biogenico_tco2,
            "metodo": f"{escolhido.metodo} {escolhido.metodo_versao}",
            "gwp": "AR5, 100 anos"
            if escolhido.gwp_conjunto == "AR5_100"
            else escolhido.gwp_conjunto,
            "conhecido_em": _hora(escolhido.conhecido_em),
            "calculado_em": _hora(escolhido.calculado_em),
            "hash_entrada": bytes(escolhido.hash_entrada).hex(),
            "hash_resultado": bytes(escolhido.hash_resultado).hex(),
            "por_categoria": por_categoria,
            "por_gas": _somar(linhas, "gas"),
            "grafico": _grafico(por_categoria),
            "pendencias": pendencias,
        }
        if pendencias:
            primeira = pendencias[0]
            contexto["proximo"] = {
                "texto": f"Resolva as {len(pendencias)} pendências, começando por"
                f" {primeira['fonte']}: {primeira['acao'].lower()} Depois, calcule de novo.",
                "url": primeira["url"],
                "rotulo": "Resolver",
            }
        else:
            contexto["proximo"] = {
                "texto": "Cálculo sem pendências. O próximo passo é o fechamento do inventário.",
                "url": None,
                "rotulo": "",
            }
    return renderizar(
        request, "paginas/resultados/indice.html", portal, pagina_atual="/resultados", **contexto
    )


@router.get("", response_class=HTMLResponse)
async def indice(request: Request, sessao: SessaoTenant, acesso: Leitor) -> HTMLResponse:
    portal = await montar_portal(request)
    inventario = None
    if portal.empresa_atual is not None:
        inventario = await coleta.inventario_em_foco(sessao, UUID(portal.empresa_atual))
    return await _tela_do_calculo(request, sessao, acesso, inventario, None)


@router.get("/calculos/{calculo_id}", response_class=HTMLResponse)
async def ver_calculo(
    request: Request, calculo_id: UUID, sessao: SessaoTenant, acesso: Leitor
) -> HTMLResponse:
    inventario_id = await sessao.scalar(
        text("SELECT inventario_id FROM calculo WHERE calculo_id = :id"), {"id": calculo_id}
    )
    if inventario_id is None:
        raise AcessoNegado
    inventario = await coleta.obter_inventario(sessao, inventario_id)
    return await _tela_do_calculo(request, sessao, acesso, inventario, calculo_id)


@router.post("/inventarios/{inventario_id}/calcular")
async def calcular(inventario_id: UUID, sessao: SessaoTenant, acesso: Revisor) -> Response:
    inventario = await coleta.obter_inventario(sessao, inventario_id)
    if not inventario.em_coleta:
        raise AcessoNegado
    try:
        execucao = await calculo.calcular_inventario(
            await conexao_asyncpg(sessao), acesso.tenant_id, inventario_id, acesso.usuario_id
        )
    except calculo.InventarioInexistente as erro:
        raise AcessoNegado from erro
    resultado = execucao.resultado
    total = formatar_numero(resultado.escopo1_tco2e, 3)
    pendencias = len(resultado.pendencias)
    total2 = formatar_numero(resultado.escopo2_tco2e, 3)
    total3 = formatar_numero(resultado.escopo3_tco2e, 3)
    aviso = (
        f"Cálculo feito: {total} tCO₂e no escopo 1, {total2} tCO₂e no escopo 2 e"
        f" {total3} tCO₂e no escopo 3"
    )
    aviso += f", com {pendencias} pendência(s)." if pendencias else ", sem pendências."
    return resposta_com_aviso(
        aviso, **{"HX-Redirect": f"/resultados/calculos/{execucao.calculo_id}"}
    )


@router.post("/calculos/{calculo_id}/conferir")
async def conferir(calculo_id: UUID, sessao: SessaoTenant, _acesso: Revisor) -> Response:
    try:
        conferencia = await calculo.refazer(await conexao_asyncpg(sessao), calculo_id)
    except calculo.InventarioInexistente as erro:
        raise AcessoNegado from erro
    if conferencia.ok:
        aviso = "Conferido: refazendo a conta, o resultado e os dois hashes são os mesmos."
    elif not conferencia.entrada_integra:
        aviso = "Atenção: a entrada guardada não confere com o hash registrado. Avise o suporte."
    else:
        aviso = "Atenção: refazendo a conta, o resultado mudou. Avise o suporte."
    return resposta_com_aviso(aviso)


async def resposta_da_memoria(sessao: Any, calculo_id: UUID) -> Response:
    """CSV da memoria de calculo (tambem usado pelo verificador, com o RLS dele)."""
    linhas = await _linhas_do_calculo(sessao, calculo_id)
    existe = await sessao.scalar(
        text("SELECT count(*) FROM calculo WHERE calculo_id = :id"), {"id": calculo_id}
    )
    if not existe:
        raise AcessoNegado
    conteudo = gerar_csv(
        CABECALHO_MEMORIA,
        (
            (
                linha.linha,
                linha.fonte,
                rotulo_periodo(linha.periodo),
                linha.categoria,
                linha.atividade,
                linha.componente,
                _decimal_csv(linha.percentual_mistura),
                linha.data_referencia.strftime("%d/%m/%Y"),
                _decimal_csv(linha.quantidade),
                linha.unidade,
                _decimal_csv(linha.quantidade_convertida),
                linha.gas,
                "sim" if linha.biogenico else "nao",
                _decimal_csv(linha.fator),
                linha.unidade_fator,
                linha.fator_versao_id,
                _decimal_csv(linha.gwp),
                linha.gwp_versao_id or "",
                _decimal_csv(linha.percentual_consolidado),
                _decimal_csv(linha.massa_t),
                _decimal_csv(linha.tco2e),
            )
            for linha in linhas
        ),
    )
    return Response(
        content=conteudo,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="memoria-{calculo_id}.csv"'},
    )


@router.get("/calculos/{calculo_id}/memoria.csv")
async def memoria(calculo_id: UUID, sessao: SessaoTenant, _acesso: Leitor) -> Response:
    return await resposta_da_memoria(sessao, calculo_id)
