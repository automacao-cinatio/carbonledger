"""Tela de fechamento: condicoes, fechar (com o nome da empresa) e reapresentar.

Leitura para todos os perfis. Fechar e reapresentar: administrador e gestor. A confirmacao
por digitacao e conferida tambem no servidor (campo confirmacao).
"""

from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from app.api.dependencias import SessaoTenant, conexao_asyncpg
from app.api.paginas import resposta_com_aviso
from app.core.security.acesso import Acesso, Perfil, exigir_perfil
from app.modules.cobranca.bloqueio import exigir_em_dia
from app.modules.coleta import servico as coleta
from app.modules.fechamento import servico as fechamento
from app.modules.organizacao import estrutura
from app.web.portal import montar_portal, renderizar

router = APIRouter(prefix="/fechamento", tags=["fechamento"], include_in_schema=False)

FUSO = ZoneInfo("America/Sao_Paulo")

Leitor = Annotated[Acesso, Depends(exigir_perfil(*Perfil))]
Gestao = Annotated[Acesso, Depends(exigir_perfil(Perfil.ADMINISTRADOR, Perfil.GESTOR))]

NOMES_CHECAGEM = {
    "calculo": "Cálculo feito",
    "atualizado": "Cálculo com os dados de agora",
    "pendencias": "Sem pendências",
    "conferido": "Cálculo conferido",
}
COLUNAS_CHECAGENS = [
    {"chave": "nome", "titulo": "Condição"},
    {"chave": "situacao", "titulo": "Situação", "etiqueta": True},
    {"chave": "detalhe", "titulo": "Detalhe"},
]
COLUNAS_EVENTOS = [
    {"chave": "quando", "titulo": "Quando"},
    {"chave": "tipo", "titulo": "Evento"},
    {"chave": "versao", "titulo": "Versão", "unidade": "nº", "casas": 0},
    {"chave": "detalhe", "titulo": "Detalhe"},
]


@router.get("", response_class=HTMLResponse)
async def indice(request: Request, sessao: SessaoTenant, acesso: Leitor) -> HTMLResponse:
    portal = await montar_portal(request)
    inventario = None
    if portal.empresa_atual is not None:
        inventario = await coleta.inventario_em_foco(sessao, UUID(portal.empresa_atual))
    contexto: dict[str, Any] = {
        "inventario": inventario,
        "empresa": None,
        "situacao": None,
        "colunas_checagens": COLUNAS_CHECAGENS,
        "colunas_eventos": COLUNAS_EVENTOS,
        "checagens": [],
        "eventos": [],
        "pode_decidir": acesso.perfil.ve_o_tenant_inteiro,
    }
    if inventario is None:
        contexto["proximo"] = (
            "Abra o inventário e colete os dados.",
            "/coleta",
            "Ir para a coleta",
        )
        return renderizar(
            request,
            "paginas/fechamento/indice.html",
            portal,
            pagina_atual="/fechamento",
            **contexto,
        )
    empresa = await estrutura.obter_empresa(sessao, inventario.empresa_id)
    situacao = await fechamento.situacao(await conexao_asyncpg(sessao), inventario.inventario_id)
    contexto["empresa"] = empresa
    contexto["situacao"] = situacao
    contexto["checagens"] = [
        {
            "nome": NOMES_CHECAGEM[c.nome],
            "situacao": ("Ok", "success") if c.ok else ("Falta", "danger"),
            "detalhe": c.detalhe,
        }
        for c in situacao.checagens
    ]
    contexto["eventos"] = [
        {
            "quando": e.registrado_em.astimezone(FUSO).strftime("%d/%m/%Y %H:%M"),
            "tipo": "Fechamento" if e.tipo == "fechamento" else "Reapresentação",
            "versao": e.versao,
            "detalhe": (
                f"Hash {e.hash_fechamento.hex()[:16]}"
                if e.hash_fechamento is not None
                else (e.motivo or "")
            ),
        }
        for e in reversed(situacao.eventos)
    ]
    if situacao.estado == "fechado":
        contexto["proximo"] = (
            "Inventário fechado. O próximo passo é a verificação por terceiro.",
            None,
            "",
        )
    elif situacao.pode_fechar:
        contexto["proximo"] = (
            "Tudo pronto. Feche o inventário: depois disso, os dados só mudam por reapresentação.",
            None,
            "",
        )
    else:
        primeira = next(c for c in situacao.checagens if not c.ok)
        destino = "/coleta" if primeira.nome == "pendencias" else "/resultados"
        contexto["proximo"] = (primeira.detalhe, destino, "Resolver")
    return renderizar(
        request, "paginas/fechamento/indice.html", portal, pagina_atual="/fechamento", **contexto
    )


async def _formulario(request: Request) -> dict[str, str]:
    return {chave: str(valor) for chave, valor in (await request.form()).items()}


@router.post("/inventarios/{inventario_id}/fechar")
async def fechar(
    request: Request, inventario_id: UUID, sessao: SessaoTenant, acesso: Gestao
) -> Response:
    inventario = await coleta.obter_inventario(sessao, inventario_id)
    await exigir_em_dia(sessao)
    empresa = await estrutura.obter_empresa(sessao, inventario.empresa_id)
    evento = await fechamento.fechar(
        await conexao_asyncpg(sessao),
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        inventario_id=inventario_id,
        confirmacao=(await _formulario(request)).get("confirmacao", ""),
        nome_empresa=empresa.nome,
    )
    return resposta_com_aviso(
        f"Inventário {inventario.ano} fechado (versão {evento.versao}).", **{"HX-Refresh": "true"}
    )


@router.post("/inventarios/{inventario_id}/reapresentar")
async def reapresentar(
    request: Request, inventario_id: UUID, sessao: SessaoTenant, acesso: Gestao
) -> Response:
    await coleta.obter_inventario(sessao, inventario_id)
    await fechamento.reabrir(
        await conexao_asyncpg(sessao),
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        inventario_id=inventario_id,
        motivo=(await _formulario(request)).get("motivo", ""),
    )
    return resposta_com_aviso(
        "Inventário reaberto para reapresentação. O fechamento anterior continua guardado.",
        **{"HX-Refresh": "true"},
    )
