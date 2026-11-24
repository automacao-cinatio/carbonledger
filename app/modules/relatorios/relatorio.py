"""Relatorio do inventario fechado: HTML, PDF (Playwright) e assinatura PAdES (pyHanko).

O relatorio retrata um fechamento (versao e hash) e os numeros do calculo daquele fechamento;
nada e recalculado aqui. A empresa assina com o proprio certificado: a assinatura atesta quem
emitiu e que o arquivo nao mudou; nao e verificacao nem certificacao das emissoes.

Roda na requisicao, com a conexao asyncpg da sessao (contexto do tenant aplicado). O Chromium
abre o HTML sem JavaScript e sem rede: tudo o que a pagina usa vem embutido.
Nivel da assinatura: PAdES B-B (sem carimbo do tempo nem dados de revogacao embutidos).
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from playwright.async_api import Route, async_playwright
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.sign import fields, signers

from app.core import audit, keys
from app.core.armazenamento import ArmazemObjetos
from app.core.erros import AcessoNegado, ErroValidacao
from app.modules.coleta.categorias import rotulo
from app.modules.coleta.evidencias import chave_objeto
from app.modules.fechamento import servico as fechamento
from app.modules.organizacao.limites import ROTULOS_ABORDAGEM, Abordagem
from app.modules.organizacao.validacao import formatar_cnpj
from app.modules.relatorios import certificados
from app.web.renderizacao import PASTA_ESTATICOS, templates

FUSO = ZoneInfo("America/Sao_Paulo")
TEMPLATE = "relatorios/inventario_pdf.html"
CAMPO_ASSINATURA = "AssinaturaDaEmpresa"
RODAPE = (
    '<div style="font-size:8px;width:100%;text-align:center">'
    'Página <span class="pageNumber"></span> de <span class="totalPages"></span></div>'
)


@dataclass(frozen=True)
class Relatorio:
    relatorio_id: UUID
    inventario_id: UUID
    versao_fechamento: int
    hash_fechamento: bytes
    certificado_id: UUID
    titular: str
    sha256: bytes
    tamanho: int
    gerado_em: datetime


def _hora(valor: datetime) -> str:
    return valor.astimezone(FUSO).strftime("%d/%m/%Y %H:%M")


def _somar(linhas: list[Any], chave: str) -> list[dict[str, Any]]:
    totais: dict[str, Decimal] = {}
    for linha in linhas:
        if linha["tco2e"] is None:
            continue
        totais[linha[chave]] = totais.get(linha[chave], Decimal(0)) + linha["tco2e"]
    return [
        {"nome": rotulo(nome) if chave == "categoria" else nome, "valor": valor}
        for nome, valor in sorted(totais.items(), key=lambda par: (-par[1], par[0]))
    ]


async def dados_do_relatorio(conexao: Any, inventario_id: UUID) -> dict[str, Any]:
    """Tudo o que o relatorio mostra, lido do fechamento atual. Sem fechamento, recusa."""
    situacao = await fechamento.situacao(conexao, inventario_id)
    atual = situacao.fechamento_atual
    if atual is None or atual.calculo_id is None or atual.hash_fechamento is None:
        raise ErroValidacao("inventario", "O relatório só sai de inventário fechado.")
    inventario = await conexao.fetchrow(
        "SELECT i.ano, i.abordagem, e.nome, e.cnpj FROM inventario i JOIN empresa e"
        " ON e.tenant_id = i.tenant_id AND e.empresa_id = i.empresa_id"
        " WHERE i.inventario_id = $1",
        inventario_id,
    )
    calculo = await conexao.fetchrow(
        "SELECT metodo, metodo_versao, gwp_conjunto, conhecido_em, hash_entrada,"
        " hash_resultado, escopo1_tco2e, escopo2_tco2e, escopo3_tco2e, biogenico_tco2"
        " FROM calculo WHERE calculo_id = $1",
        atual.calculo_id,
    )
    if inventario is None or calculo is None:
        raise AcessoNegado
    linhas = list(
        await conexao.fetch(
            "SELECT categoria, gas, tco2e FROM calculo_linha WHERE calculo_id = $1",
            atual.calculo_id,
        )
    )
    unidades = await conexao.fetch(
        "SELECT nome, municipio, uf, participacao_societaria FROM unidade"
        " WHERE empresa_id = $1 AND ativa ORDER BY nome",
        situacao.empresa_id,
    )
    return {
        "empresa": inventario["nome"],
        "cnpj": formatar_cnpj(inventario["cnpj"]) or "Não informado",
        "ano": inventario["ano"],
        "abordagem": ROTULOS_ABORDAGEM[Abordagem(inventario["abordagem"])],
        "empresa_id": situacao.empresa_id,
        "versao": atual.versao,
        "fechado_em": _hora(atual.registrado_em),
        "hash_fechamento": atual.hash_fechamento,
        "calculo_id": atual.calculo_id,
        "metodo": f"{calculo['metodo']} {calculo['metodo_versao']}",
        "gwp": "AR5, 100 anos" if calculo["gwp_conjunto"] == "AR5_100" else calculo["gwp_conjunto"],
        "conhecido_em": _hora(calculo["conhecido_em"]),
        "hash_entrada": bytes(calculo["hash_entrada"]).hex(),
        "hash_resultado": bytes(calculo["hash_resultado"]).hex(),
        "escopo1": calculo["escopo1_tco2e"],
        "escopo2": calculo["escopo2_tco2e"],
        "escopo3": calculo["escopo3_tco2e"],
        "biogenico": calculo["biogenico_tco2"],
        "por_categoria": _somar(linhas, "categoria"),
        "por_gas": _somar(linhas, "gas"),
        "unidades": [dict(u) for u in unidades],
    }


def montar_html(dados: dict[str, Any], certificado: certificados.Certificado) -> str:
    tema = (PASTA_ESTATICOS / "css" / "tema.css").read_text(encoding="utf-8")
    return templates.env.get_template(TEMPLATE).render(
        **dados,
        hash_fechamento_hex=dados["hash_fechamento"].hex(),
        tema=tema,
        assinante=certificado.titular,
        cnpj_assinante=formatar_cnpj(certificado.cnpj) if certificado.cnpj else None,
    )


async def _bloquear(rota: Route) -> None:
    await rota.abort()


async def gerar_pdf(html: str) -> bytes:
    """HTML em PDF A4 no Chromium, sem JavaScript e sem rede."""
    async with async_playwright() as playwright:
        navegador = await playwright.chromium.launch()
        try:
            contexto = await navegador.new_context(java_script_enabled=False)
            pagina = await contexto.new_page()
            await pagina.route("**/*", _bloquear)
            await pagina.set_content(html, wait_until="load")
            return await pagina.pdf(
                format="A4",
                print_background=True,
                margin={"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"},
                display_header_footer=True,
                header_template="<span></span>",
                footer_template=RODAPE,
            )
        finally:
            await navegador.close()


async def assinar_pdf(pdf: bytes, pkcs12_sem_senha: bytes, *, titular: str, motivo: str) -> bytes:
    """Assinatura PAdES (B-B) com SHA-256, num campo novo, por atualizacao incremental."""
    assinante = signers.SimpleSigner.load_pkcs12_data(pkcs12_sem_senha, other_certs=[])
    if assinante is None:
        raise ValueError("certificado sem chave para assinar")
    escritor = IncrementalPdfFileWriter(BytesIO(pdf), strict=False)
    metadados = signers.PdfSignatureMetadata(
        field_name=CAMPO_ASSINATURA,
        md_algorithm="sha256",
        subfilter=fields.SigSeedSubFilter.PADES,
        reason=motivo,
        location="Brasil",
        name=titular,
    )
    saida = await signers.async_sign_pdf(escritor, metadados, signer=assinante)
    return bytes(saida.getvalue())


async def gerar(
    conexao: Any,
    *,
    tenant_id: UUID,
    usuario_id: UUID,
    inventario_id: UUID,
    certificado: certificados.Certificado,
    provedor: keys.ProvedorChaves,
    armazem: ArmazemObjetos,
) -> UUID:
    """Gera, assina, guarda no S3 (Object Lock) e registra. So de inventario fechado."""
    dados = await dados_do_relatorio(conexao, inventario_id)
    if certificado.empresa_id != dados["empresa_id"]:
        raise AcessoNegado
    if not certificado.vigente():
        raise ErroValidacao(
            "certificado", "O certificado da empresa está vencido ou desativado. Envie outro."
        )
    pdf = await gerar_pdf(montar_html(dados, certificado))
    pkcs12 = await certificados.pkcs12_para_assinar(
        conexao, provedor, tenant_id, certificado.certificado_id
    )
    assinado = await assinar_pdf(
        pdf,
        pkcs12,
        titular=certificado.titular,
        motivo=f"Inventário de emissões {dados['ano']}, fechamento versão {dados['versao']}",
    )
    sha256 = hashlib.sha256(assinado).digest()
    relatorio_id = uuid4()
    guardado = await armazem.guardar(
        chave_objeto(tenant_id, dados["empresa_id"], "relatorios", relatorio_id),
        assinado,
        sha256,
        "application/pdf",
    )
    await conexao.execute(
        "INSERT INTO relatorio_inventario (tenant_id, relatorio_id, empresa_id, inventario_id,"
        " versao_fechamento, hash_fechamento, certificado_id, sha256, tamanho, chave_objeto,"
        " versao_objeto, gerado_por) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
        tenant_id,
        relatorio_id,
        dados["empresa_id"],
        inventario_id,
        dados["versao"],
        dados["hash_fechamento"],
        certificado.certificado_id,
        sha256,
        len(assinado),
        guardado.chave,
        guardado.versao,
        usuario_id,
    )
    await audit.registrar_asyncpg(
        conexao,
        tenant_id=tenant_id,
        usuario_id=usuario_id,
        acao="relatorio.gerado",
        entidade="relatorio_inventario",
        entidade_id=relatorio_id,
        dados={
            "empresa_id": str(dados["empresa_id"]),
            "inventario_id": str(inventario_id),
            "versao_fechamento": dados["versao"],
            "hash_fechamento": dados["hash_fechamento"].hex(),
            "certificado_id": str(certificado.certificado_id),
            "sha256": sha256.hex(),
        },
    )
    return relatorio_id


_SQL_RELATORIOS = (
    "SELECT r.relatorio_id, r.inventario_id, r.versao_fechamento, r.hash_fechamento,"
    " r.certificado_id, c.titular, r.sha256, r.tamanho, r.gerado_em"
    " FROM relatorio_inventario r JOIN certificado_assinatura c"
    "   ON c.tenant_id = r.tenant_id AND c.certificado_id = r.certificado_id"
)


def _relatorio(linha: Any) -> Relatorio:
    return Relatorio(
        relatorio_id=linha["relatorio_id"],
        inventario_id=linha["inventario_id"],
        versao_fechamento=linha["versao_fechamento"],
        hash_fechamento=bytes(linha["hash_fechamento"]),
        certificado_id=linha["certificado_id"],
        titular=linha["titular"],
        sha256=bytes(linha["sha256"]),
        tamanho=linha["tamanho"],
        gerado_em=linha["gerado_em"],
    )


async def listar(conexao: Any, inventario_id: UUID) -> list[Relatorio]:
    return [
        _relatorio(linha)
        for linha in await conexao.fetch(
            _SQL_RELATORIOS + " WHERE r.inventario_id = $1 ORDER BY r.gerado_em DESC",
            inventario_id,
        )
    ]


async def ler(
    conexao: Any, relatorio_id: UUID, armazem: ArmazemObjetos
) -> tuple[Relatorio, int, bytes]:
    """O PDF guardado, so se ainda for o mesmo que foi registrado. Devolve tambem o ano."""
    linha = await conexao.fetchrow(
        _SQL_RELATORIOS.replace("SELECT ", "SELECT r.chave_objeto, r.versao_objeto, i.ano, ", 1)
        + " JOIN inventario i ON i.tenant_id = r.tenant_id AND i.inventario_id = r.inventario_id"
        " WHERE r.relatorio_id = $1",
        relatorio_id,
    )
    if linha is None:
        raise AcessoNegado
    conteudo = await armazem.ler(linha["chave_objeto"], linha["versao_objeto"])
    if hashlib.sha256(conteudo).digest() != bytes(linha["sha256"]):
        raise RuntimeError("relatorio com hash diferente do registrado")
    return _relatorio(linha), linha["ano"], conteudo
