"""Sessoes no Redis e estado temporario do login OIDC.

O cookie leva um token aleatorio; o Redis guarda so o hash dele. Quem ler o Redis nao
consegue montar um cookie valido. A sessao expira por inatividade e por idade maxima.
"""

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

PREFIXO_SESSAO = "sessao:"
PREFIXO_LOGIN = "login_oidc:"
VALIDADE_LOGIN_S = 600


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "replace")).hexdigest()


def tokens_iguais(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


@dataclass(frozen=True)
class Sessao:
    chave: str
    usuario_id: UUID
    tenant_id: UUID | None
    csrf: str
    criada_em: float
    # Empresa em foco no seletor do topo (so interface; o acesso vem do perfil).
    empresa_id: UUID | None = None

    def como_json(self) -> str:
        return json.dumps(
            {
                "usuario_id": str(self.usuario_id),
                "tenant_id": None if self.tenant_id is None else str(self.tenant_id),
                "csrf": self.csrf,
                "criada_em": self.criada_em,
                "empresa_id": None if self.empresa_id is None else str(self.empresa_id),
            }
        )


@dataclass(frozen=True)
class ArmazemSessoes:
    redis: Redis
    inatividade_s: int
    maxima_s: int

    def _ttl(self, criada_em: float) -> int:
        restante = int(criada_em + self.maxima_s - time.time())
        return max(0, min(self.inatividade_s, restante))

    async def criar(self, usuario_id: UUID, tenant_id: UUID | None) -> tuple[str, Sessao]:
        """Sessao nova a cada login (nunca reaproveita token: evita fixacao de sessao)."""
        token = secrets.token_urlsafe(32)
        sessao = Sessao(
            chave=PREFIXO_SESSAO + _hash(token),
            usuario_id=usuario_id,
            tenant_id=tenant_id,
            csrf=secrets.token_urlsafe(32),
            criada_em=time.time(),
        )
        await self.redis.set(sessao.chave, sessao.como_json(), ex=self._ttl(sessao.criada_em))
        return token, sessao

    async def obter(self, token: str | None) -> Sessao | None:
        if not token or len(token) > 128:
            return None
        chave = PREFIXO_SESSAO + _hash(token)
        bruto = await self.redis.get(chave)
        if bruto is None:
            return None
        dados = json.loads(bruto)
        sessao = Sessao(
            chave=chave,
            usuario_id=UUID(dados["usuario_id"]),
            tenant_id=None if dados["tenant_id"] is None else UUID(dados["tenant_id"]),
            csrf=dados["csrf"],
            criada_em=float(dados["criada_em"]),
            empresa_id=UUID(dados["empresa_id"]) if dados.get("empresa_id") else None,
        )
        ttl = self._ttl(sessao.criada_em)
        if ttl <= 0:
            await self.redis.delete(chave)
            return None
        await self.redis.expire(chave, ttl)
        return sessao

    async def _gravar(self, sessao: Sessao) -> Sessao:
        ttl = self._ttl(sessao.criada_em)
        if ttl > 0:
            await self.redis.set(sessao.chave, sessao.como_json(), ex=ttl)
        return sessao

    async def trocar_tenant(self, sessao: Sessao, tenant_id: UUID | None) -> Sessao:
        """Trocar de cliente sempre limpa a empresa em foco."""
        return await self._gravar(replace(sessao, tenant_id=tenant_id, empresa_id=None))

    async def trocar_empresa(self, sessao: Sessao, empresa_id: UUID | None) -> Sessao:
        return await self._gravar(replace(sessao, empresa_id=empresa_id))

    async def encerrar(self, sessao: Sessao) -> None:
        await self.redis.delete(sessao.chave)

    async def guardar_login(self, state: str, dados: dict[str, Any]) -> None:
        await self.redis.set(PREFIXO_LOGIN + _hash(state), json.dumps(dados), ex=VALIDADE_LOGIN_S)

    async def consumir_login(self, state: str) -> dict[str, Any] | None:
        """Le e apaga numa so operacao: o mesmo state nao serve duas vezes."""
        bruto = await self.redis.getdel(PREFIXO_LOGIN + _hash(state))
        if bruto is None:
            return None
        dados: dict[str, Any] = json.loads(bruto)
        return dados
