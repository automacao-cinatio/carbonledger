"""Relatorios: certificado da empresa e relatorio do inventario fechado em PDF assinado (P19).

Leitura e download para todos os perfis (o RLS limita as empresas). Certificado: so o
administrador envia e desativa. Gerar o relatorio: administrador e gestor, so de inventario
fechado e com o certificado da empresa vigente. Sem chaves configuradas (KeyProvider), nada
de certificado nem de assinatura: a tela avisa.
"""

from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from starlette.datastructures import UploadFile

from app.api.dependencias import SessaoTenant, conexao_asyncpg
from app.api.paginas import resposta_com_aviso
from app.core import keys
from app.core.antivirus import Antivirus
from app.core.armazenamento import ArmazemObjetos
from app.core.config import obter_configuracao
from app.core.erros import AcessoNegado, ErroValidacao
from app.core.security.acesso import Acesso, Perfil, exigir_perfil
from app.modules.cobranca.bloqueio import exigir_em_dia
from app.modules.coleta import servico as coleta
from app.modules.fechamento import servico as fechamento
from app.modules.organizacao import estrutura
from app.modules.organizacao.validacao import formatar_cnpj
from app.modules.relatorios import certificados, relatorio
from app.web.portal import montar_portal, renderizar

router = APIRouter(prefix="/relatorios", tags=["relatorios"], include_in_schema=False)

FUSO = ZoneInfo("America/Sao_Paulo")
DIAS_AVISO_VENCIMENTO = 30

Leitor = Annotated[Acesso, Depends(exigir_perfil(*Perfil))]
Gestao = Annotated[Acesso, Depends(exigir_perfil(Perfil.ADMINISTRADOR, Perfil.GESTOR))]
Administrador = Annotated[Acesso, Depends(exigir_perfil(Perfil.ADMINISTRADOR))]

COLUNAS_RELATORIOS = [
    {"chave": "gerado_em", "titulo": "Gerado em", "link": "url"},
    {"chave": "versao", "titulo": "Fechamento", "unidade": "versão", "casas": 0},
    {"chave": "assinante", "titulo": "Assinado por"},
    {"chave": "carimbo", "titulo": "Carimbo do tempo"},
    {"chave": "tamanho", "titulo": "Tamanho", "unidade": "KB", "casas": 0},
    {"chave": "hash", "titulo": "SHA-256 (início)"},
    {"chave": "situacao", "titulo": "Retrata", "etiqueta": True},
]


def _hora(valor: datetime) -> str:
    return valor.astimezone(FUSO).strftime("%d/%m/%Y %H:%M")


def _chaves(request: Request) -> keys.ProvedorChaves | None:
    return getattr(request.app.state, "chaves", None)


def _armazem(request: Request) -> ArmazemObjetos:
    armazem: ArmazemObjetos = request.app.state.armazem
    return armazem


def _antivirus(request: Request) -> Antivirus:
    antivirus: Antivirus = request.app.state.antivirus
    return antivirus


def _situacao_certificado(certificado: certificados.Certificado) -> tuple[str, str]:
    agora = datetime.now(UTC)
    if not certificado.vigente(agora):
        return ("Vencido", "danger")
    if (certificado.valido_ate - agora).days < DIAS_AVISO_VENCIMENTO:
        return ("Vence em breve", "warning")
    return ("Vigente", "success")


def _tela(request: Request, portal: Any, contexto: dict[str, Any]) -> HTMLResponse:
    return renderizar(
        request, "paginas/relatorios/indice.html", portal, pagina_atual="/relatorios", **contexto
    )


@router.get("", response_class=HTMLResponse)
async def indice(request: Request, sessao: SessaoTenant, acesso: Leitor) -> HTMLResponse:
    portal = await montar_portal(request)
    contexto: dict[str, Any] = {
        "empresa": None,
        "inventario": None,
        "certificado": None,
        "situacao_certificado": None,
        "fechamento_atual": None,
        "relatorios": [],
        "colunas_relatorios": COLUNAS_RELATORIOS,
        "chaves_configuradas": _chaves(request) is not None,
        "pode_certificado": acesso.perfil == Perfil.ADMINISTRADOR,
        "pode_gerar": False,
    }
    if portal.empresa_atual is None:
        contexto["proximo"] = ("Cadastre a empresa na organização.", "/organizacao", "Ir")
        return _tela(request, portal, contexto)
    empresa_id = UUID(portal.empresa_atual)
    empresa = await estrutura.obter_empresa(sessao, empresa_id)
    certificado = await certificados.ativo_da_empresa(sessao, empresa_id)
    inventario = await coleta.inventario_em_foco(sessao, empresa_id)
    contexto.update(empresa=empresa, inventario=inventario, certificado=certificado)
    if certificado is not None:
        contexto["situacao_certificado"] = _situacao_certificado(certificado)
        contexto["certificado_ficha"] = [
            ("Titular", certificado.titular),
            ("CNPJ do certificado", formatar_cnpj(certificado.cnpj) or "Sem CNPJ ICP-Brasil"),
            ("Emitido por", certificado.emissor),
            ("Válido de", _hora(certificado.valido_de)),
            ("Válido até", _hora(certificado.valido_ate)),
            ("Impressão digital (SHA-256)", certificado.impressao_sha256.hex()),
        ]
    atual = None
    if inventario is not None:
        conexao = await conexao_asyncpg(sessao)
        situacao = await fechamento.situacao(conexao, inventario.inventario_id)
        atual = situacao.fechamento_atual
        contexto["fechamento_atual"] = atual
        contexto["relatorios"] = [
            {
                "gerado_em": _hora(r.gerado_em),
                "url": f"/relatorios/{r.relatorio_id}/arquivo.pdf",
                "versao": r.versao_fechamento,
                "assinante": r.titular,
                "carimbo": _hora(r.carimbo_em) if r.carimbo_em else "Sem carimbo",
                "tamanho": max(1, (r.tamanho + 1023) // 1024),
                "hash": r.sha256.hex()[:16],
                "situacao": ("Fechamento atual", "success")
                if atual is not None and r.hash_fechamento == atual.hash_fechamento
                else ("Fechamento anterior", "secondary"),
            }
            for r in await relatorio.listar(conexao, inventario.inventario_id)
        ]
    contexto["pode_gerar"] = (
        acesso.perfil.ve_o_tenant_inteiro
        and atual is not None
        and certificado is not None
        and certificado.vigente()
        and _chaves(request) is not None
    )
    if inventario is None or atual is None:
        contexto["proximo"] = (
            "O relatório sai do inventário fechado. Feche o inventário primeiro.",
            "/fechamento",
            "Ir para o fechamento",
        )
    elif certificado is None or not certificado.vigente():
        contexto["proximo"] = (
            "Peça ao administrador que envie o certificado A1 da empresa, aqui nesta tela.",
            None,
            "",
        )
    elif not contexto["relatorios"]:
        contexto["proximo"] = ("Gere o relatório assinado do inventário fechado.", None, "")
    else:
        contexto["proximo"] = (
            "Envie o relatório assinado ao verificador, com o acesso da tela Verificação.",
            "/verificacao",
            "Ir para a verificação",
        )
    return _tela(request, portal, contexto)


async def _ler_certificado(request: Request) -> tuple[str | None, bytes, str]:
    declarado = request.headers.get("content-length", "")
    if declarado.isdigit() and int(declarado) > certificados.LIMITE_BYTES + 16 * 1024:
        raise ErroValidacao("arquivo", "O arquivo é grande demais para um certificado A1.")
    formulario = await request.form(max_files=1, max_fields=3, max_part_size=16 * 1024)
    arquivo = formulario.get("arquivo")
    if not isinstance(arquivo, UploadFile) or not arquivo.filename:
        raise ErroValidacao("arquivo", "Escolha o arquivo do certificado (.pfx ou .p12).")
    conteudo = await arquivo.read(certificados.LIMITE_BYTES + 1)
    senha = formulario.get("senha")
    return arquivo.filename, conteudo, senha if isinstance(senha, str) else ""


@router.post("/empresas/{empresa_id}/certificado")
async def enviar_certificado(
    request: Request, empresa_id: UUID, sessao: SessaoTenant, acesso: Administrador
) -> Response:
    provedor = _chaves(request)
    if provedor is None:
        raise AcessoNegado
    empresa = await estrutura.obter_empresa(sessao, empresa_id)
    nome, conteudo, senha = await _ler_certificado(request)
    await certificados.cadastrar(
        sessao,
        acesso,
        empresa_id,
        empresa.cnpj,
        nome,
        conteudo,
        senha,
        provedor=provedor,
        antivirus=_antivirus(request),
        exigir_cnpj=obter_configuracao().em_producao,
    )
    return resposta_com_aviso(
        "Certificado guardado. A senha não foi guardada.", **{"HX-Refresh": "true"}
    )


@router.post("/certificados/{certificado_id}/desativar")
async def desativar_certificado(
    certificado_id: UUID, sessao: SessaoTenant, acesso: Administrador
) -> Response:
    await certificados.desativar(sessao, acesso, certificado_id)
    return resposta_com_aviso(
        "Certificado desativado. A chave foi apagada.", **{"HX-Refresh": "true"}
    )


@router.post("/inventarios/{inventario_id}/gerar")
async def gerar(
    request: Request, inventario_id: UUID, sessao: SessaoTenant, acesso: Gestao
) -> Response:
    provedor = _chaves(request)
    if provedor is None:
        raise AcessoNegado
    inventario = await coleta.obter_inventario(sessao, inventario_id)
    await exigir_em_dia(sessao)
    certificado = await certificados.ativo_da_empresa(sessao, inventario.empresa_id)
    if certificado is None:
        raise ErroValidacao("certificado", "Envie o certificado A1 da empresa antes de gerar.")
    await relatorio.gerar(
        await conexao_asyncpg(sessao),
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        inventario_id=inventario_id,
        certificado=certificado,
        provedor=provedor,
        armazem=_armazem(request),
        carimbadora=relatorio.carimbadora_da_configuracao(obter_configuracao()),
    )
    return resposta_com_aviso("Relatório gerado e assinado.", **{"HX-Refresh": "true"})


@router.get("/{relatorio_id}/arquivo.pdf")
async def baixar(
    request: Request, relatorio_id: UUID, sessao: SessaoTenant, _acesso: Leitor
) -> Response:
    dados, ano, conteudo = await relatorio.ler(
        await conexao_asyncpg(sessao), relatorio_id, _armazem(request)
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
