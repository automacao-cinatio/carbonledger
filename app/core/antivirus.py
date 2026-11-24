"""Cliente do ClamAV (clamd, protocolo INSTREAM). Falha fechada.

Se o ClamAV nao responde, responde algo inesperado ou acha ameaca, o arquivo e recusado: nada
e guardado sem verificacao.
"""

import asyncio
import contextlib
import struct
from typing import Protocol

TAMANHO_BLOCO = 64 * 1024


class AntivirusIndisponivel(RuntimeError):
    """O ClamAV nao respondeu como esperado: o envio e recusado."""


class ArquivoInfectado(ValueError):
    """O ClamAV achou ameaca no arquivo."""


class Antivirus(Protocol):
    async def verificar(self, conteudo: bytes) -> None: ...


class ClienteClamav:
    def __init__(self, host: str, porta: int, tempo_limite_s: float) -> None:
        self.host = host
        self.porta = porta
        self.tempo_limite_s = tempo_limite_s

    async def _conversar(self, conteudo: bytes) -> bytes:
        leitor, escritor = await asyncio.open_connection(self.host, self.porta)
        try:
            escritor.write(b"zINSTREAM\0")
            for inicio in range(0, len(conteudo), TAMANHO_BLOCO):
                bloco = conteudo[inicio : inicio + TAMANHO_BLOCO]
                escritor.write(struct.pack("!I", len(bloco)) + bloco)
                await escritor.drain()
            escritor.write(struct.pack("!I", 0))
            await escritor.drain()
            return await leitor.read(4096)
        finally:
            escritor.close()
            with contextlib.suppress(OSError):
                await escritor.wait_closed()

    async def verificar(self, conteudo: bytes) -> None:
        try:
            resposta = await asyncio.wait_for(self._conversar(conteudo), self.tempo_limite_s)
        except (OSError, TimeoutError) as erro:
            raise AntivirusIndisponivel(type(erro).__name__) from erro
        texto = resposta.rstrip(b"\0\n").decode("utf-8", errors="replace")
        if texto.endswith(" FOUND"):
            raise ArquivoInfectado
        if texto != "stream: OK":
            # "size limit exceeded", erro interno ou resposta vazia: nada passa sem veredito.
            raise AntivirusIndisponivel("resposta inesperada do ClamAV")
