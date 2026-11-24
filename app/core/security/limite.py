"""Limite de tentativas de login por IP (Redis), com janela fixa.

Conta o inicio de cada login (entrar, convite, concessao): quem passa do limite espera a
janela acabar. O IP nao vai para o log; a chave no Redis guarda so o hash dele.
"""

import hashlib
from typing import Any

PREFIXO = "limite_login:"


def _chave(ip: str) -> str:
    return PREFIXO + hashlib.sha256(ip.encode("utf-8", "replace")).hexdigest()


async def contar_tentativa(redis: Any, ip: str, *, limite: int, janela_s: int) -> bool:
    """Registra a tentativa e devolve True se ainda esta dentro do limite."""
    chave = _chave(ip)
    total = int(await redis.incr(chave))
    if total == 1:
        await redis.expire(chave, janela_s)
    return total <= limite
