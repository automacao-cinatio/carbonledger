"""Leitura e verificacao da trilha de auditoria do tenant (administrador e gestor)."""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from app.api.dependencias import SessaoTenant
from app.core import audit
from app.core.security.acesso import Acesso, Perfil, exigir_perfil

router = APIRouter(prefix="/api/auditoria", tags=["auditoria"])

Leitor = Annotated[Acesso, Depends(exigir_perfil(Perfil.ADMINISTRADOR, Perfil.GESTOR))]

LIMITE_MAXIMO = 500


@router.get("")
async def listar_eventos(
    sessao: SessaoTenant,
    _acesso: Leitor,
    apos: Annotated[int, Query(ge=0, description="ultimo seq ja lido")] = 0,
    limite: Annotated[int, Query(ge=1, le=LIMITE_MAXIMO)] = 100,
) -> list[dict[str, Any]]:
    linhas = await sessao.execute(
        text(
            "SELECT seq, evento_id, ocorrido_em, usuario_id, acao, entidade, entidade_id,"
            " dados::text AS dados_texto, hash FROM audit_log"
            " WHERE seq > :apos ORDER BY seq LIMIT :limite"
        ),
        {"apos": apos, "limite": limite},
    )
    return [
        {
            "seq": linha.seq,
            "evento_id": str(linha.evento_id),
            "ocorrido_em": linha.ocorrido_em.isoformat(),
            "usuario_id": None if linha.usuario_id is None else str(linha.usuario_id),
            "acao": linha.acao,
            "entidade": linha.entidade,
            "entidade_id": linha.entidade_id,
            "dados": json.loads(linha.dados_texto),
            "hash": linha.hash.hex(),
        }
        for linha in linhas
    ]


@router.get("/verificacao")
async def verificar(sessao: SessaoTenant, acesso: Leitor) -> dict[str, Any]:
    resultado = await audit.verificar_tenant(sessao, acesso.tenant_id)
    return {
        "integra": resultado.integra,
        "eventos": resultado.eventos,
        "quebra_em": resultado.quebra_em,
        "motivo": resultado.motivo,
    }
