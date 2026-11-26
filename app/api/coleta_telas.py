"""Telas de coleta: inventario, fontes de emissao, lancamentos, aprovacao e evidencias.

Leitura para todos os perfis (o RLS limita as empresas). Abrir inventario: administrador e
gestor. Fontes e aprovacao: administrador, gestor e responsavel. Lancar dado, enviar evidencia
e importar planilha: todos. Acoes por HTMX: 204 com aviso e HX-Redirect ou HX-Refresh; erro de
campo com 422.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from starlette.datastructures import UploadFile

from app.api.dependencias import SessaoTenant, conexao_asyncpg
from app.api.ia_telas import ia_do_cliente
from app.api.paginas import resposta_com_aviso
from app.core import email as servico_email
from app.core.antivirus import Antivirus
from app.core.armazenamento import ArmazemObjetos
from app.core.config import obter_configuracao
from app.core.erros import AcessoNegado, ErroValidacao
from app.core.security.acesso import Acesso, Perfil, exigir_perfil
from app.modules.cobranca.bloqueio import exigir_em_dia
from app.modules.coleta import campanhas, evidencias, planilha, servico
from app.modules.coleta.categorias import CATEGORIAS, periodos, rotulo, rotulo_periodo
from app.modules.organizacao import estrutura
from app.modules.organizacao.limites import ROTULOS_ABORDAGEM, Abordagem
from app.web.portal import montar_portal, renderizar
from app.web.renderizacao import templates

router = APIRouter(prefix="/coleta", tags=["coleta"], include_in_schema=False)

FUSO = ZoneInfo("America/Sao_Paulo")

Leitor = Annotated[Acesso, Depends(exigir_perfil(*Perfil))]
Gestao = Annotated[Acesso, Depends(exigir_perfil(Perfil.ADMINISTRADOR, Perfil.GESTOR))]
Revisor = Annotated[
    Acesso,
    Depends(exigir_perfil(Perfil.ADMINISTRADOR, Perfil.GESTOR, Perfil.RESPONSAVEL)),
]

PERFIS_REVISORES = (Perfil.ADMINISTRADOR, Perfil.GESTOR, Perfil.RESPONSAVEL)

OPCOES_ABORDAGEM = [(a.value, ROTULOS_ABORDAGEM[a]) for a in Abordagem]
OPCOES_CATEGORIA = [(c.codigo, f"Escopo {c.escopo}: {c.rotulo}") for c in CATEGORIAS]
OPCOES_PERIODICIDADE = [("mensal", "Mensal (um dado por mês)"), ("anual", "Anual (um dado no ano)")]

SITUACOES = {
    None: ("Falta lançar", "secondary"),
    "aguardando": ("Aguardando aprovação", "warning"),
    "aprovado": ("Aprovado", "success"),
    "devolvido": ("Devolvido para correção", "danger"),
}

COLUNAS_INVENTARIOS = [
    {"chave": "ano", "titulo": "Inventário", "link": "url"},
    {"chave": "abordagem", "titulo": "Abordagem de consolidação"},
    {"chave": "estado", "titulo": "Estado", "etiqueta": True},
]
COLUNAS_FONTES = [
    {"chave": "descricao", "titulo": "Fonte", "link": "url"},
    {"chave": "unidade", "titulo": "Unidade"},
    {"chave": "categoria", "titulo": "Categoria"},
    {"chave": "aprovados", "titulo": "Dados aprovados", "unidade": "períodos", "casas": 0},
    {"chave": "esperados", "titulo": "Dados esperados", "unidade": "períodos", "casas": 0},
    {"chave": "com_evidencia", "titulo": "Com evidência", "unidade": "períodos", "casas": 0},
    {"chave": "situacao", "titulo": "Situação", "etiqueta": True},
]
COLUNAS_EVIDENCIAS = [
    {"chave": "nome", "titulo": "Arquivo", "link": "url"},
    {"chave": "periodo", "titulo": "Período"},
    {"chave": "tamanho", "titulo": "Tamanho", "unidade": "KB", "casas": 0},
    {"chave": "enviado_em", "titulo": "Enviado em"},
    {"chave": "hash", "titulo": "SHA-256 (início)"},
]


@dataclass(frozen=True)
class ProximoPasso:
    texto: str
    url: str | None = None
    rotulo: str = "Continuar"


def hoje() -> date:
    return datetime.now(FUSO).date()


def _armazem(request: Request) -> ArmazemObjetos:
    armazem: ArmazemObjetos = request.app.state.armazem
    return armazem


def _antivirus(request: Request) -> Antivirus:
    antivirus: Antivirus = request.app.state.antivirus
    return antivirus


def _limite_bytes() -> int:
    return obter_configuracao().upload_limite_mb * 1024 * 1024


def _pode_revisar(acesso: Acesso) -> bool:
    return acesso.perfil in PERFIS_REVISORES


async def _formulario(request: Request) -> dict[str, str]:
    return {chave: str(valor) for chave, valor in (await request.form()).items()}


async def _ler_upload(request: Request) -> tuple[str | None, bytes, dict[str, str]]:
    """Um arquivo (campo "arquivo") e poucos campos de texto, com limite de tamanho."""
    limite = _limite_bytes()
    declarado = request.headers.get("content-length", "")
    if declarado.isdigit() and int(declarado) > limite + 64 * 1024:
        raise ErroValidacao(
            "arquivo", f"O arquivo passa de {limite // (1024 * 1024)} MB. Envie um arquivo menor."
        )
    formulario = await request.form(max_files=1, max_fields=5, max_part_size=64 * 1024)
    arquivo = formulario.get("arquivo")
    if not isinstance(arquivo, UploadFile) or not arquivo.filename:
        raise ErroValidacao("arquivo", "Escolha o arquivo a enviar.")
    conteudo = await arquivo.read(limite + 1)
    campos = {k: str(v) for k, v in formulario.items() if not isinstance(v, UploadFile)}
    return arquivo.filename, conteudo, campos


async def _tela(request: Request, template: str, **contexto: Any) -> HTMLResponse:
    return renderizar(
        request, template, await montar_portal(request), pagina_atual="/coleta", **contexto
    )


def _casas(valor: Decimal) -> int:
    """Casas para mostrar o numero sem perder digitos (entre 2 e 6)."""
    expoente = valor.normalize().as_tuple().exponent
    return max(2, min(-expoente, servico.CASAS_MAXIMAS)) if isinstance(expoente, int) else 2


def _url_fonte(fonte_id: UUID) -> str:
    return f"/coleta/fontes/{fonte_id}"


def _url_periodo(fonte_id: UUID, periodo: int) -> str:
    return f"/coleta/fontes/{fonte_id}/periodos/{periodo}"


# ---------------------------------------------------------------- inventarios


@router.get("", response_class=HTMLResponse)
async def indice(request: Request, sessao: SessaoTenant, acesso: Leitor) -> HTMLResponse:
    portal = await montar_portal(request)
    if portal.empresa_atual is None:
        return renderizar(
            request,
            "paginas/coleta/indice.html",
            portal,
            pagina_atual="/coleta",
            empresa=None,
            linhas=[],
            colunas=COLUNAS_INVENTARIOS,
            pode_abrir=False,
            valores={},
            abordagens=OPCOES_ABORDAGEM,
            proximo=ProximoPasso(
                "Cadastre a empresa e as unidades antes de abrir o inventário.",
                "/organizacao",
                "Ir para a organização",
            ),
        )
    empresa = await estrutura.obter_empresa(sessao, UUID(portal.empresa_atual))
    inventarios = await servico.listar_inventarios(sessao, empresa.empresa_id)
    linhas = [
        {
            "ano": f"Inventário {i.ano}",
            "url": f"/coleta/inventarios/{i.inventario_id}",
            "abordagem": ROTULOS_ABORDAGEM[i.abordagem],
            "estado": (i.estado_rotulo, "primary"),
        }
        for i in inventarios
    ]
    # Sugestao: o ano que acabou; se ja tem inventario, o anterior mais recente sem inventario.
    anos = {i.ano for i in inventarios}
    ano_sugerido = hoje().year - 1
    while ano_sugerido in anos:
        ano_sugerido -= 1
    pode_abrir = acesso.perfil.ve_o_tenant_inteiro and empresa.ativa
    if inventarios and inventarios[0].em_coleta:
        atual = inventarios[0]
        proximo = ProximoPasso(
            f"Continue a coleta do inventário {atual.ano}.",
            f"/coleta/inventarios/{atual.inventario_id}",
            f"Abrir o inventário {atual.ano}",
        )
    elif inventarios:
        atual = inventarios[0]
        proximo = ProximoPasso(
            f"Inventário {atual.ano} fechado: os dados só mudam por reapresentação. Gere o"
            " relatório assinado e dê acesso ao verificador.",
            "/relatorios",
            "Ir para os relatórios",
        )
    elif pode_abrir:
        proximo = ProximoPasso(f"Abra o inventário de {ano_sugerido} no formulário acima.")
    else:
        proximo = ProximoPasso("Peça ao administrador ou ao gestor que abra o inventário do ano.")
    return renderizar(
        request,
        "paginas/coleta/indice.html",
        portal,
        pagina_atual="/coleta",
        empresa=empresa,
        linhas=linhas,
        colunas=COLUNAS_INVENTARIOS,
        pode_abrir=pode_abrir,
        valores={"ano": str(ano_sugerido), "abordagem": Abordagem.CONTROLE_OPERACIONAL.value},
        abordagens=OPCOES_ABORDAGEM,
        proximo=proximo,
    )


@router.post("/empresas/{empresa_id}/inventarios")
async def criar_inventario(
    request: Request, empresa_id: UUID, sessao: SessaoTenant, acesso: Gestao
) -> Response:
    await exigir_em_dia(sessao)
    formulario = await _formulario(request)
    inventario_id = await servico.criar_inventario(sessao, acesso, empresa_id, formulario, hoje())
    return resposta_com_aviso(
        "Inventário aberto.", **{"HX-Redirect": f"/coleta/inventarios/{inventario_id}"}
    )


def _proximo_do_inventario(
    inventario: servico.Inventario,
    situacoes: list[servico.SituacaoFonte],
    acesso: Acesso,
) -> ProximoPasso:
    contam = [s for s in situacoes if s.fonte.conta_no_progresso]
    if not contam:
        return ProximoPasso(
            "Cadastre as fontes de emissão de cada unidade: o que é queimado, consumido ou"
            " vazado e entra no inventário.",
            f"/coleta/inventarios/{inventario.inventario_id}/fontes/nova"
            if _pode_revisar(acesso)
            else None,
            "Cadastrar fonte",
        )
    for situacao in contam:
        for periodo, versao in sorted(situacao.atuais.items()):
            if versao.decisao == "devolvido":
                return ProximoPasso(
                    f"Corrija o dado devolvido de {situacao.fonte.descricao}"
                    f" ({rotulo_periodo(periodo)}).",
                    _url_periodo(situacao.fonte.fonte_id, periodo),
                    "Corrigir",
                )
    for situacao in contam:
        for periodo in periodos(situacao.fonte.periodicidade):
            if periodo not in situacao.atuais:
                return ProximoPasso(
                    f"Lance o dado de {situacao.fonte.descricao} ({rotulo_periodo(periodo)}),"
                    " ou importe a planilha de coleta com vários dados de uma vez.",
                    _url_periodo(situacao.fonte.fonte_id, periodo),
                    "Lançar dado",
                )
    if _pode_revisar(acesso):
        for situacao in contam:
            for periodo, versao in sorted(situacao.atuais.items()):
                if versao.decisao is None and versao.criado_por != acesso.usuario_id:
                    return ProximoPasso(
                        f"Confira e aprove o dado de {situacao.fonte.descricao}"
                        f" ({rotulo_periodo(periodo)}).",
                        _url_periodo(situacao.fonte.fonte_id, periodo),
                        "Conferir",
                    )
    for situacao in contam:
        if situacao.progresso.com_evidencia < situacao.progresso.esperados:
            return ProximoPasso(
                f"Anexe as evidências de {situacao.fonte.descricao}: notas fiscais, contas ou"
                " relatórios que comprovem os dados.",
                _url_fonte(situacao.fonte.fonte_id),
                "Anexar evidências",
            )
    total = servico.progresso_total(situacoes)
    if total.aprovados < total.esperados:
        return ProximoPasso(
            "Faltam aprovações. Peça a outra pessoa com perfil de responsável, gestor ou"
            " administrador que confira os dados que você lançou."
        )
    return ProximoPasso(
        "Coleta completa, com todos os dados aprovados e comprovados. O próximo passo é o"
        " cálculo das emissões, na etapa de resultados."
    )


@router.get("/inventarios/{inventario_id}", response_class=HTMLResponse)
async def ver_inventario(
    request: Request, inventario_id: UUID, sessao: SessaoTenant, acesso: Leitor
) -> HTMLResponse:
    inventario = await servico.obter_inventario(sessao, inventario_id)
    empresa = await estrutura.obter_empresa(sessao, inventario.empresa_id)
    situacoes = await servico.situacao_das_fontes(sessao, inventario_id)
    conexao = await conexao_asyncpg(sessao)
    campanha = await campanhas.aberta(conexao, inventario_id)
    envios = (
        []
        if campanha is None
        else [
            {
                "tipo": TIPOS_ENVIO[e["tipo"]],
                "quando": e["enviado_em"].astimezone(FUSO).strftime("%d/%m/%Y %H:%M"),
                "destinatarios": e["destinatarios"],
            }
            for e in await campanhas.envios(conexao, campanha.campanha_id)
        ]
    )
    linhas = [
        {
            "descricao": s.fonte.descricao,
            "url": _url_fonte(s.fonte.fonte_id),
            "unidade": s.fonte.unidade_nome,
            "categoria": rotulo(s.fonte.categoria),
            "aprovados": s.progresso.aprovados,
            "esperados": s.progresso.esperados,
            "com_evidencia": s.progresso.com_evidencia,
            "situacao": (
                ("Desativada", "secondary")
                if not s.fonte.ativa
                else ("Fora da materialidade", "secondary")
                if not s.fonte.material
                else ("Unidade fora do limite", "secondary")
                if not s.fonte.dentro_do_limite
                else ("Completa", "success")
                if s.progresso.aprovados == s.progresso.esperados
                else ("Em coleta", "primary")
            ),
        }
        for s in situacoes
    ]
    return await _tela(
        request,
        "paginas/coleta/inventario.html",
        inventario=inventario,
        empresa=empresa,
        abordagem=ROTULOS_ABORDAGEM[inventario.abordagem],
        total=servico.progresso_total(situacoes),
        colunas=COLUNAS_FONTES,
        linhas=linhas,
        pode_fontes=_pode_revisar(acesso) and inventario.em_coleta,
        pode_campanha=acesso.perfil.ve_o_tenant_inteiro and inventario.em_coleta,
        campanha=campanha,
        envios=envios,
        colunas_envios=COLUNAS_ENVIOS,
        amanha=(hoje() + timedelta(days=1)).isoformat(),
        proximo=_proximo_do_inventario(inventario, situacoes, acesso),
    )


TIPOS_ENVIO = {
    "abertura": "Abertura",
    "lembrete_7": "Lembrete (7 dias)",
    "lembrete_3": "Lembrete (3 dias)",
    "lembrete_1": "Lembrete (1 dia)",
    "vencida": "Prazo vencido",
}
COLUNAS_ENVIOS = [
    {"chave": "tipo", "titulo": "Aviso"},
    {"chave": "quando", "titulo": "Enviado em"},
    {"chave": "destinatarios", "titulo": "Destinatários", "unidade": "pessoas", "casas": 0},
]


async def _enviar_email(para: str, assunto: str, corpo: str) -> None:
    await servico_email.enviar_email(obter_configuracao(), para, assunto, corpo)


@router.post("/inventarios/{inventario_id}/campanha")
async def abrir_campanha(
    request: Request, inventario_id: UUID, sessao: SessaoTenant, acesso: Gestao
) -> Response:
    await servico.obter_inventario(sessao, inventario_id)
    _campanha, avisados = await campanhas.abrir(
        await conexao_asyncpg(sessao),
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        inventario_id=inventario_id,
        formulario=await _formulario(request),
        hoje=hoje(),
        enviar=_enviar_email,
    )
    return resposta_com_aviso(
        f"Campanha aberta. {avisados} pessoa(s) avisada(s) por e-mail.", **{"HX-Refresh": "true"}
    )


@router.post("/campanhas/{campanha_id}/encerrar")
async def encerrar_campanha(campanha_id: UUID, sessao: SessaoTenant, acesso: Gestao) -> Response:
    await campanhas.encerrar(
        await conexao_asyncpg(sessao),
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        campanha_id=campanha_id,
        motivo="manual",
    )
    return resposta_com_aviso("Campanha encerrada. Os lembretes param.", **{"HX-Refresh": "true"})


@router.get("/inventarios/{inventario_id}/planilha.csv")
async def baixar_planilha(inventario_id: UUID, sessao: SessaoTenant, _acesso: Leitor) -> Response:
    inventario = await servico.obter_inventario(sessao, inventario_id)
    conteudo = await planilha.gerar_modelo(sessao, inventario_id)
    nome = f"coleta-{inventario.ano}.csv"
    return Response(
        content=conteudo,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


@router.post("/inventarios/{inventario_id}/planilha")
async def importar_planilha(
    request: Request, inventario_id: UUID, sessao: SessaoTenant, acesso: Leitor
) -> Response:
    nome, conteudo, _campos = await _ler_upload(request)
    gravadas = await planilha.importar_planilha(
        sessao,
        acesso,
        inventario_id,
        nome,
        conteudo,
        armazem=_armazem(request),
        antivirus=_antivirus(request),
        limite_bytes=_limite_bytes(),
        hoje=hoje(),
    )
    if gravadas == 0:
        aviso = "Planilha conferida: nenhum dado novo ou alterado."
    elif gravadas == 1:
        aviso = "Planilha importada: 1 dado gravado, aguardando aprovação."
    else:
        aviso = f"Planilha importada: {gravadas} dados gravados, aguardando aprovação."
    return resposta_com_aviso(aviso, **{"HX-Refresh": "true"})


# ---------------------------------------------------------------- fontes


async def _opcoes_atividade(sessao: SessaoTenant, categoria: str | None) -> list[tuple[str, str]]:
    """Atividades com fator aprovado. Sem categoria (fonte nova), as de todas, com o nome dela."""
    if categoria is not None:
        return list((await servico.atividades_da_categoria(sessao, categoria)).items())
    opcoes: dict[str, str] = {}
    for c in CATEGORIAS:
        for codigo, rotulo_atividade in (
            await servico.atividades_da_categoria(sessao, c.codigo)
        ).items():
            opcoes.setdefault(codigo, f"{c.rotulo}: {rotulo_atividade}")
    return sorted(opcoes.items(), key=lambda par: par[1].lower())


def _valores_fonte(fonte: servico.Fonte | None) -> dict[str, Any]:
    if fonte is None:
        return {
            "unidade_id": None,
            "categoria": None,
            "descricao": "",
            "unidade_medida": "",
            "periodicidade": "mensal",
            "material": True,
            "justificativa": "",
            "atividade": None,
        }
    return {
        "atividade": fonte.atividade,
        "unidade_id": str(fonte.unidade_id),
        "categoria": fonte.categoria,
        "descricao": fonte.descricao,
        "unidade_medida": fonte.unidade_medida,
        "periodicidade": fonte.periodicidade,
        "material": fonte.material,
        "justificativa": fonte.justificativa or "",
    }


@router.get("/inventarios/{inventario_id}/fontes/nova", response_class=HTMLResponse)
async def nova_fonte(
    request: Request, inventario_id: UUID, sessao: SessaoTenant, _acesso: Revisor
) -> HTMLResponse:
    inventario = await servico.obter_inventario(sessao, inventario_id)
    if not inventario.em_coleta:
        raise AcessoNegado
    empresa = await estrutura.obter_empresa(sessao, inventario.empresa_id)
    unidades = [
        (str(u.unidade_id), u.nome)
        for u in await estrutura.listar_unidades(sessao, empresa.empresa_id)
        if u.ativa
    ]
    return await _tela(
        request,
        "paginas/coleta/fonte_form.html",
        titulo="Nova fonte de emissão",
        acao=f"/coleta/inventarios/{inventario_id}/fontes",
        rascunho=f"fonte-nova-{inventario_id}",
        inventario=inventario,
        empresa=empresa,
        fonte=None,
        unidades=unidades,
        categorias=OPCOES_CATEGORIA,
        periodicidades=OPCOES_PERIODICIDADE,
        atividades=await _opcoes_atividade(sessao, None),
        valores=_valores_fonte(None),
        ia_disponivel=await ia_do_cliente(request, sessao) is not None,
    )


@router.post("/inventarios/{inventario_id}/fontes")
async def criar_fonte(
    request: Request, inventario_id: UUID, sessao: SessaoTenant, acesso: Revisor
) -> Response:
    fonte_id = await servico.criar_fonte(sessao, acesso, inventario_id, await _formulario(request))
    return resposta_com_aviso("Fonte cadastrada.", **{"HX-Redirect": _url_fonte(fonte_id)})


@router.get("/fontes/{fonte_id}", response_class=HTMLResponse)
async def ver_fonte(
    request: Request, fonte_id: UUID, sessao: SessaoTenant, acesso: Leitor
) -> HTMLResponse:
    fonte = await servico.obter_fonte(sessao, fonte_id)
    inventario = await servico.obter_inventario(sessao, fonte.inventario_id)
    situacao = (await servico.situacao_das_fontes(sessao, inventario.inventario_id, fonte_id))[0]
    periodos_fonte = periodos(fonte.periodicidade)
    linhas = []
    for periodo in periodos_fonte:
        atual = situacao.atuais.get(periodo)
        linhas.append(
            {
                "periodo": rotulo_periodo(periodo),
                "url": _url_periodo(fonte_id, periodo),
                "quantidade": atual.quantidade if atual else None,
                "situacao": SITUACOES[atual.situacao if atual else None],
                "evidencias": situacao.evidencias_do_periodo(periodo),
            }
        )
    colunas = [
        {"chave": "periodo", "titulo": "Período", "link": "url"},
        {
            "chave": "quantidade",
            "titulo": "Quantidade",
            "unidade": fonte.unidade_medida,
            "exato": True,
        },
        {"chave": "situacao", "titulo": "Situação", "etiqueta": True},
        {"chave": "evidencias", "titulo": "Evidências", "unidade": "arquivos", "casas": 0},
    ]
    lista = await evidencias.listar_evidencias(sessao, fonte_id)
    linhas_evidencias = [
        {
            "nome": e.nome_arquivo,
            "url": f"/coleta/evidencias/{e.evidencia_id}/arquivo",
            "periodo": "Todos" if e.periodo is None else rotulo_periodo(e.periodo),
            "tamanho": max(1, (e.tamanho + 1023) // 1024),
            "enviado_em": e.enviado_em.astimezone(FUSO).strftime("%d/%m/%Y %H:%M"),
            "hash": e.sha256_hex[:12],
        }
        for e in lista
    ]
    opcoes_periodo = [("", "Todos os períodos")] + [
        (str(p), rotulo_periodo(p)) for p in periodos_fonte
    ]
    faltando = next((p for p in periodos_fonte if p not in situacao.atuais), None)
    if not fonte.ativa:
        proximo = ProximoPasso("Fonte desativada: ela não entra no inventário.")
    elif faltando is not None:
        proximo = ProximoPasso(
            f"Lance o dado de {rotulo_periodo(faltando)}.",
            _url_periodo(fonte_id, faltando),
            "Lançar dado",
        )
    elif situacao.progresso.com_evidencia < situacao.progresso.esperados:
        proximo = ProximoPasso(
            "Anexe as evidências dos períodos que ainda não têm, no formulário acima."
        )
    else:
        proximo = ProximoPasso(
            "Volte ao inventário para ver as outras fontes.",
            f"/coleta/inventarios/{inventario.inventario_id}",
            "Voltar ao inventário",
        )
    return await _tela(
        request,
        "paginas/coleta/fonte.html",
        fonte=fonte,
        inventario=inventario,
        categoria=rotulo(fonte.categoria),
        atividade=(await servico.atividades_da_categoria(sessao, fonte.categoria)).get(
            fonte.atividade or "", fonte.atividade
        ),
        progresso=situacao.progresso,
        colunas=colunas,
        linhas=linhas,
        colunas_evidencias=COLUNAS_EVIDENCIAS,
        linhas_evidencias=linhas_evidencias,
        opcoes_periodo=opcoes_periodo,
        pode_editar=_pode_revisar(acesso) and inventario.em_coleta,
        pode_enviar=inventario.em_coleta and fonte.ativa,
        proximo=proximo,
    )


@router.get("/fontes/{fonte_id}/editar", response_class=HTMLResponse)
async def editar_fonte(
    request: Request, fonte_id: UUID, sessao: SessaoTenant, _acesso: Revisor
) -> HTMLResponse:
    fonte = await servico.obter_fonte(sessao, fonte_id)
    inventario = await servico.obter_inventario(sessao, fonte.inventario_id)
    if not inventario.em_coleta:
        raise AcessoNegado
    empresa = await estrutura.obter_empresa(sessao, inventario.empresa_id)
    return await _tela(
        request,
        "paginas/coleta/fonte_form.html",
        titulo=f"Editar {fonte.descricao}",
        acao=f"/coleta/fontes/{fonte_id}",
        rascunho=f"fonte-{fonte_id}",
        inventario=inventario,
        empresa=empresa,
        fonte=fonte,
        unidades=[(str(fonte.unidade_id), fonte.unidade_nome)],
        categorias=OPCOES_CATEGORIA,
        periodicidades=OPCOES_PERIODICIDADE,
        atividades=await _opcoes_atividade(sessao, fonte.categoria),
        ia_disponivel=False,
        valores=_valores_fonte(fonte),
    )


@router.post("/fontes/{fonte_id}")
async def salvar_fonte(
    request: Request, fonte_id: UUID, sessao: SessaoTenant, acesso: Revisor
) -> Response:
    await servico.alterar_fonte(sessao, acesso, fonte_id, await _formulario(request))
    return resposta_com_aviso("Fonte salva.", **{"HX-Redirect": _url_fonte(fonte_id)})


@router.post("/fontes/{fonte_id}/desativar")
async def desativar_fonte(fonte_id: UUID, sessao: SessaoTenant, acesso: Revisor) -> Response:
    await servico.definir_fonte_ativa(sessao, acesso, fonte_id, ativa=False)
    return resposta_com_aviso("Fonte desativada.", **{"HX-Refresh": "true"})


@router.post("/fontes/{fonte_id}/reativar")
async def reativar_fonte(fonte_id: UUID, sessao: SessaoTenant, acesso: Revisor) -> Response:
    await servico.definir_fonte_ativa(sessao, acesso, fonte_id, ativa=True)
    return resposta_com_aviso("Fonte reativada.", **{"HX-Refresh": "true"})


# ---------------------------------------------------------------- periodo e lancamentos


@router.get("/fontes/{fonte_id}/periodos/{periodo}", response_class=HTMLResponse)
async def ver_periodo(
    request: Request, fonte_id: UUID, periodo: int, sessao: SessaoTenant, acesso: Leitor
) -> HTMLResponse:
    fonte = await servico.obter_fonte(sessao, fonte_id)
    if periodo not in periodos(fonte.periodicidade):
        raise AcessoNegado
    inventario = await servico.obter_inventario(sessao, fonte.inventario_id)
    versoes = await servico.historico(sessao, fonte_id, periodo)
    atual = versoes[0] if versoes else None
    colunas = [
        {"chave": "versao", "titulo": "Versão", "unidade": "nº", "casas": 0},
        {
            "chave": "quantidade",
            "titulo": "Quantidade",
            "unidade": fonte.unidade_medida,
            "exato": True,
        },
        {"chave": "data_compra", "titulo": "Data da compra"},
        {"chave": "origem", "titulo": "Origem"},
        {"chave": "criado_em", "titulo": "Lançado em"},
        {"chave": "situacao", "titulo": "Situação", "etiqueta": True},
    ]
    linhas = [
        {
            "versao": v.versao,
            "quantidade": v.quantidade,
            "data_compra": v.data_compra.strftime("%d/%m/%Y") if v.data_compra else "",
            "origem": "Planilha" if v.origem == "planilha" else "Formulário",
            "criado_em": v.criado_em.astimezone(FUSO).strftime("%d/%m/%Y %H:%M"),
            "situacao": SITUACOES[v.situacao],
        }
        for v in versoes
    ]
    pode_lancar = inventario.em_coleta and fonte.ativa
    pode_decidir = (
        atual is not None
        and atual.decisao is None
        and inventario.em_coleta
        and _pode_revisar(acesso)
        and atual.criado_por != acesso.usuario_id
    )
    if atual is None:
        proximo = ProximoPasso("Lance o dado do período no formulário acima.")
    elif atual.decisao == "devolvido":
        proximo = ProximoPasso(
            "Corrija o dado no formulário acima: a correção vira uma versão nova."
        )
    elif atual.decisao is None and pode_decidir:
        proximo = ProximoPasso("Confira o dado com a evidência e aprove ou devolva.")
    elif atual.decisao is None:
        proximo = ProximoPasso(
            "O dado aguarda a aprovação de outra pessoa. Enquanto isso, anexe a evidência.",
            _url_fonte(fonte_id),
            "Ir para as evidências",
        )
    else:
        proximo = ProximoPasso(
            "Dado aprovado. Volte à fonte para os outros períodos.",
            _url_fonte(fonte_id),
            "Voltar à fonte",
        )
    valores = {
        "quantidade": atual.quantidade if atual else None,
        "casas": _casas(atual.quantidade) if atual else 2,
        "data_compra": atual.data_compra.isoformat() if atual and atual.data_compra else "",
        "observacao": (atual.observacao or "") if atual else "",
    }
    return await _tela(
        request,
        "paginas/coleta/periodo.html",
        fonte=fonte,
        inventario=inventario,
        periodo=periodo,
        rotulo_periodo=rotulo_periodo(periodo),
        atual=atual,
        situacao=SITUACOES[atual.situacao if atual else None],
        colunas=colunas,
        linhas=linhas,
        valores=valores,
        pode_lancar=pode_lancar,
        pode_decidir=pode_decidir,
        autor_e_voce=atual is not None and atual.criado_por == acesso.usuario_id,
        ia_disponivel=await ia_do_cliente(request, sessao) is not None,
        proximo=proximo,
    )


@router.post("/fontes/{fonte_id}/periodos/{periodo}/lancamentos")
async def lancar(
    request: Request, fonte_id: UUID, periodo: int, sessao: SessaoTenant, acesso: Leitor
) -> Response:
    formulario = {**(await _formulario(request)), "periodo": str(periodo)}
    _fonte, _periodo, gravou = await servico.lancar(sessao, acesso, fonte_id, formulario, hoje())
    aviso = (
        "Dado gravado. Ele aguarda a aprovação de outra pessoa."
        if gravou
        else "Nada mudou: o dado é igual ao que já estava gravado."
    )
    return resposta_com_aviso(aviso, **{"HX-Refresh": "true"})


@router.post("/lancamentos/{lancamento_id}/aprovar")
async def aprovar(lancamento_id: UUID, sessao: SessaoTenant, acesso: Revisor) -> Response:
    await servico.decidir(sessao, acesso, lancamento_id, aprovar=True)
    return resposta_com_aviso("Dado aprovado.", **{"HX-Refresh": "true"})


@router.post("/lancamentos/{lancamento_id}/devolver")
async def devolver(
    request: Request, lancamento_id: UUID, sessao: SessaoTenant, acesso: Revisor
) -> Response:
    motivo = (await _formulario(request)).get("motivo", "")
    await servico.decidir(sessao, acesso, lancamento_id, aprovar=False, motivo=motivo)
    return resposta_com_aviso("Dado devolvido para correção.", **{"HX-Refresh": "true"})


# ---------------------------------------------------------------- evidencias


@router.post("/fontes/{fonte_id}/evidencias")
async def enviar_evidencia(
    request: Request, fonte_id: UUID, sessao: SessaoTenant, acesso: Leitor
) -> Response:
    nome, conteudo, campos = await _ler_upload(request)
    await evidencias.enviar_evidencia(
        sessao,
        acesso,
        fonte_id,
        campos.get("periodo", ""),
        nome,
        conteudo,
        armazem=_armazem(request),
        antivirus=_antivirus(request),
        limite_bytes=_limite_bytes(),
    )
    return resposta_com_aviso("Evidência guardada.", **{"HX-Refresh": "true"})


@router.get("/evidencias/{evidencia_id}/arquivo")
async def baixar_evidencia(
    request: Request, evidencia_id: UUID, sessao: SessaoTenant, _acesso: Leitor
) -> Response:
    evidencia, conteudo = await evidencias.ler_evidencia(sessao, evidencia_id, _armazem(request))
    return Response(
        content=conteudo,
        media_type=evidencia.tipo_conteudo,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(evidencia.nome_arquivo)}",
            "Cache-Control": "private, no-store",
        },
    )


# ---------------------------------------------------------------- validacao na hora

VALIDADORES = {
    "quantidade": lambda v: servico.validar_quantidade(v)[1],
    "unidade_medida": lambda v: servico.validar_unidade_medida(v)[1],
    "ano": lambda v: servico.validar_ano(v, hoje())[1],
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
