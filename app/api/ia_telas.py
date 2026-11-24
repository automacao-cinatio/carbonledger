"""Sugestoes da IA nas telas: categoria e atividade da fonte, e opiniao sobre um dado.

Respondem um trecho de HTML (HTMX) com a sugestao ou com o aviso de que a IA nao respondeu.
A IA so sugere: nada e gravado alem do registro na trilha de que ela foi consultada (sem o
conteudo). Sem IA configurada, as rotas respondem 404 e as telas nem mostram o botao.
"""

from decimal import Decimal
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from app.api.dependencias import SessaoTenant
from app.core import audit
from app.core.erros import AcessoNegado
from app.core.security.acesso import Acesso, Perfil, exigir_perfil
from app.modules.coleta import servico as coleta
from app.modules.coleta.categorias import CATEGORIAS, rotulo, rotulo_periodo
from app.modules.ia.cliente import ClienteIA, IAIndisponivel
from app.modules.ia.sugestoes import opinar_sobre_dado, sugerir_fonte
from app.web.renderizacao import templates

router = APIRouter(prefix="/ia", tags=["ia"], include_in_schema=False)

Revisor = Annotated[
    Acesso, Depends(exigir_perfil(Perfil.ADMINISTRADOR, Perfil.GESTOR, Perfil.RESPONSAVEL))
]

ROTULOS_CONFIANCA = {"baixa": "baixa", "media": "média", "alta": "alta"}
AVISO_INDISPONIVEL = (
    "A IA não respondeu agora. Siga sem a sugestão ou tente de novo em alguns minutos."
)


async def ia_do_cliente(request: Request, sessao: Any) -> ClienteIA | None:
    """A IA so vale com chave configurada e com o cliente tendo ligado (LGPD, P23)."""
    ia: ClienteIA | None = getattr(request.app.state, "ia", None)
    if ia is None:
        return None
    ligada = await sessao.scalar(text("SELECT ia_habilitada FROM tenant"))
    return ia if ligada else None


async def _ia(request: Request, sessao: Any) -> ClienteIA:
    ia = await ia_do_cliente(request, sessao)
    if ia is None:
        raise AcessoNegado
    return ia


def _resposta(request: Request, tipo: str, titulo: str, mensagem: str) -> HTMLResponse:
    resposta = templates.TemplateResponse(
        request, "partes/ia_resposta.html", {"tipo": tipo, "titulo": titulo, "mensagem": mensagem}
    )
    resposta.headers["X-Aviso"] = quote("Resposta da IA pronta. Ela é só uma sugestão.")
    return resposta


async def _registrar(sessao: Any, acesso: Acesso, uso: str, empresa_id: UUID | None) -> None:
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="ia.consultada",
        entidade="ia",
        dados={"uso": uso, "empresa_id": None if empresa_id is None else str(empresa_id)},
    )


@router.post("/fontes/sugestao", response_class=HTMLResponse)
async def sugerir_categoria(
    request: Request, sessao: SessaoTenant, acesso: Revisor
) -> HTMLResponse:
    ia = await _ia(request, sessao)
    formulario = {k: str(v) for k, v in (await request.form()).items()}
    descricao = formulario.get("descricao", "").strip()
    if not descricao:
        return _resposta(
            request, "info", "Descreva a fonte primeiro", "A IA sugere a partir da descrição."
        )
    atividades: dict[str, str] = {}
    for categoria in CATEGORIAS:
        for codigo, texto in (
            await coleta.atividades_da_categoria(sessao, categoria.codigo)
        ).items():
            atividades.setdefault(codigo, texto)
    await _registrar(sessao, acesso, "fonte", None)
    try:
        sugestao = await sugerir_fonte(
            ia,
            descricao=descricao,
            unidade_medida=formulario.get("unidade_medida", ""),
            categorias={c.codigo: f"Escopo {c.escopo}: {c.rotulo}" for c in CATEGORIAS},
            atividades=atividades,
        )
    except IAIndisponivel:
        return _resposta(request, "warning", "Sem sugestão", AVISO_INDISPONIVEL)
    partes = [f"Categoria: {rotulo(sugestao.categoria)}."]
    if sugestao.atividade:
        partes.append(f"Combustível ou atividade: {atividades[sugestao.atividade]}.")
    partes.append(f"Confiança {ROTULOS_CONFIANCA[sugestao.confianca]}. {sugestao.justificativa}")
    partes.append("Se concordar, escolha nos campos acima: nada foi preenchido por você.")
    return _resposta(request, "info", "Sugestão da IA", " ".join(partes))


@router.post("/lancamentos/{lancamento_id}/opiniao", response_class=HTMLResponse)
async def opinar(
    request: Request, lancamento_id: UUID, sessao: SessaoTenant, acesso: Revisor
) -> HTMLResponse:
    ia = await _ia(request, sessao)
    fonte_id, versao = await coleta.obter_lancamento(sessao, lancamento_id)
    fonte = await coleta.obter_fonte(sessao, fonte_id)
    situacao = (await coleta.situacao_das_fontes(sessao, fonte.inventario_id, fonte_id))[0]
    historico: list[tuple[str, Decimal]] = [
        (rotulo_periodo(periodo), dado.quantidade)
        for periodo, dado in sorted(situacao.atuais.items())
        if periodo != versao.periodo and dado.decisao != "devolvido"
    ]
    await _registrar(sessao, acesso, "dado", fonte.empresa_id)
    try:
        opiniao = await opinar_sobre_dado(
            ia,
            descricao=fonte.descricao,
            unidade=versao.unidade,
            historico=historico,
            novo=(rotulo_periodo(versao.periodo), versao.quantidade),
        )
    except IAIndisponivel:
        return _resposta(request, "warning", "Sem opinião", AVISO_INDISPONIVEL)
    if opiniao.fora_do_padrao:
        return _resposta(
            request, "warning", "A IA acha que o valor foge do padrão", opiniao.explicacao
        )
    return _resposta(request, "info", "A IA não viu nada fora do padrão", opiniao.explicacao)
