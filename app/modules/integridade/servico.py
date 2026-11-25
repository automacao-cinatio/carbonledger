"""Verificacao de integridade de um tenant (asyncpg, com o contexto de tenant aplicado).

Confere, sem confiar em quem gravou:
1. a trilha de auditoria: sequencia, encadeamento e hash de cada evento (formato v1);
2. cada calculo: refeito pela entrada guardada, com os dois hashes;
3. cada inventario fechado: o hash do fechamento atual recalculado;
4. cada evidencia, planilha importada e relatorio assinado: o objeto relido do S3 com o
   SHA-256 conferido.

O resultado e gravado (so insercao) e vai para a trilha so com as contagens. Falha vira log de
erro (sem dado pessoal) e aparece na tela de integridade. Nada e corrigido aqui: integridade
quebrada e incidente, tratado pelo runbook.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import structlog

from app.core import audit
from app.core.armazenamento import ArmazemObjetos
from app.modules.calculo import servico as calculo
from app.modules.fechamento.servico import hash_do_fechamento

log = structlog.get_logger()


@dataclass(frozen=True)
class Falha:
    tipo: str
    referencia: str
    motivo: str


@dataclass
class Relatorio:
    verificacao_id: UUID
    contagens: dict[str, int] = field(default_factory=dict)
    falhas: list[Falha] = field(default_factory=list)

    @property
    def integra(self) -> bool:
        return not self.falhas


async def _trilha(conexao: Any, tenant_id: UUID, relatorio: Relatorio) -> None:
    linhas = [
        audit.LinhaTrilha(
            **{
                **dict(linha),
                # O formato v1 compara bytes; o driver pode devolver memoryview.
                "hash_anterior": bytes(linha["hash_anterior"]),
                "hash": bytes(linha["hash"]),
            }
        )
        for linha in await conexao.fetch(
            "SELECT tenant_id, seq, evento_id, ocorrido_em, usuario_id, acao, entidade,"
            " entidade_id, dados::text AS dados_texto, hash_anterior, hash"
            " FROM audit_log WHERE tenant_id = $1 ORDER BY seq",
            tenant_id,
        )
    ]
    resultado = audit.verificar_cadeia(linhas)
    relatorio.contagens["eventos_da_trilha"] = resultado.eventos
    if not resultado.integra:
        relatorio.falhas.append(
            Falha("trilha", f"evento {resultado.quebra_em}", resultado.motivo or "cadeia rompida")
        )


async def _calculos(conexao: Any, relatorio: Relatorio) -> None:
    ids = [linha["calculo_id"] for linha in await conexao.fetch("SELECT calculo_id FROM calculo")]
    relatorio.contagens["calculos"] = len(ids)
    for calculo_id in ids:
        try:
            conferencia = await calculo.refazer(conexao, calculo_id)
        except ValueError as erro:
            relatorio.falhas.append(Falha("calculo", str(calculo_id), str(erro)))
            continue
        if not conferencia.entrada_integra:
            relatorio.falhas.append(
                Falha("calculo", str(calculo_id), "a entrada guardada não confere com o hash")
            )
        elif not conferencia.resultado_igual:
            relatorio.falhas.append(
                Falha("calculo", str(calculo_id), "refazendo a conta, o resultado mudou")
            )


async def _fechamentos(conexao: Any, relatorio: Relatorio) -> None:
    atuais = await conexao.fetch(
        "SELECT DISTINCT ON (e.inventario_id) e.inventario_id, e.versao, e.calculo_id,"
        " e.hash_fechamento FROM inventario_evento e JOIN inventario i"
        "   ON i.tenant_id = e.tenant_id AND i.inventario_id = e.inventario_id"
        " WHERE e.tipo = 'fechamento' AND i.estado = 'fechado'"
        " ORDER BY e.inventario_id, e.versao DESC"
    )
    relatorio.contagens["fechamentos"] = len(atuais)
    for linha in atuais:
        refeito = await hash_do_fechamento(
            conexao, linha["inventario_id"], linha["versao"], linha["calculo_id"]
        )
        if refeito != bytes(linha["hash_fechamento"]):
            relatorio.falhas.append(
                Falha(
                    "fechamento",
                    f"inventário {linha['inventario_id']} versão {linha['versao']}",
                    "o hash do fechamento não confere com os dados de agora",
                )
            )


async def _arquivos(
    conexao: Any, armazem: ArmazemObjetos, relatorio: Relatorio, tipo: str, sql: str
) -> None:
    linhas = await conexao.fetch(sql)
    relatorio.contagens[tipo] = len(linhas)
    for linha in linhas:
        referencia = str(linha["id"])
        try:
            conteudo = await armazem.ler(linha["chave_objeto"], linha["versao_objeto"])
        except Exception as erro:  # qualquer falha de leitura e achado de integridade
            relatorio.falhas.append(
                Falha(tipo, referencia, f"objeto inacessível no S3 ({type(erro).__name__})")
            )
            continue
        if hashlib.sha256(conteudo).digest() != bytes(linha["sha256"]):
            relatorio.falhas.append(Falha(tipo, referencia, "SHA-256 diferente do registrado"))


async def verificar(
    conexao: Any,
    tenant_id: UUID,
    armazem: ArmazemObjetos,
    *,
    origem: str,
    pedida_por: UUID | None = None,
) -> Relatorio:
    iniciada_em: datetime = await conexao.fetchval("SELECT clock_timestamp()")
    relatorio = Relatorio(uuid4())
    await _trilha(conexao, tenant_id, relatorio)
    await _calculos(conexao, relatorio)
    await _fechamentos(conexao, relatorio)
    await _arquivos(
        conexao,
        armazem,
        relatorio,
        "evidencias",
        "SELECT evidencia_id AS id, chave_objeto, versao_objeto, sha256 FROM evidencia",
    )
    await _arquivos(
        conexao,
        armazem,
        relatorio,
        "planilhas",
        "SELECT planilha_id AS id, chave_objeto, NULL::text AS versao_objeto, sha256"
        " FROM planilha_importacao",
    )
    await _arquivos(
        conexao,
        armazem,
        relatorio,
        "relatorios",
        "SELECT relatorio_id AS id, chave_objeto, versao_objeto, sha256 FROM relatorio_inventario",
    )
    falhas = [f.__dict__ for f in relatorio.falhas]
    await conexao.execute(
        "INSERT INTO integridade_verificacao (tenant_id, verificacao_id, origem, iniciada_em,"
        " contagens, falhas, integra, pedida_por)"
        " VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8)",
        tenant_id,
        relatorio.verificacao_id,
        origem,
        iniciada_em,
        json.dumps(relatorio.contagens, sort_keys=True),
        json.dumps(falhas, ensure_ascii=False),
        relatorio.integra,
        pedida_por,
    )
    await audit.registrar_asyncpg(
        conexao,
        tenant_id=tenant_id,
        usuario_id=pedida_por,
        acao="integridade.verificada",
        entidade="integridade_verificacao",
        entidade_id=relatorio.verificacao_id,
        dados={"origem": origem, "falhas": len(relatorio.falhas), **relatorio.contagens},
    )
    if relatorio.falhas:
        log.error(
            "integridade_falhou",
            tenant_id=str(tenant_id),
            falhas=len(relatorio.falhas),
            tipos=sorted({f.tipo for f in relatorio.falhas}),
        )
    return relatorio
