"""Token do convite: tenant, convite e um segredo de 32 bytes num texto so.

O banco guarda so o SHA-256 do segredo. O tenant e o convite vao no token para a busca ser
pela chave (tenant_id, convite_id), sem indice global por segredo.
"""

import base64
import binascii
import hashlib
import secrets
from dataclasses import dataclass
from uuid import UUID

TAMANHO_SEGREDO = 32
_TAMANHO_TOTAL = 16 + 16 + TAMANHO_SEGREDO


@dataclass(frozen=True)
class TokenConvite:
    tenant_id: UUID
    convite_id: UUID
    segredo: bytes

    @property
    def segredo_hash(self) -> bytes:
        return hashlib.sha256(self.segredo).digest()

    def codificar(self) -> str:
        bruto = self.tenant_id.bytes + self.convite_id.bytes + self.segredo
        return base64.urlsafe_b64encode(bruto).rstrip(b"=").decode("ascii")


def novo_token(tenant_id: UUID, convite_id: UUID) -> TokenConvite:
    return TokenConvite(tenant_id, convite_id, secrets.token_bytes(TAMANHO_SEGREDO))


def decodificar_token(texto: str) -> TokenConvite | None:
    if not texto or len(texto) > 128:
        return None
    try:
        bruto = base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(bruto) != _TAMANHO_TOTAL:
        return None
    return TokenConvite(UUID(bytes=bruto[:16]), UUID(bytes=bruto[16:32]), bruto[32:])
