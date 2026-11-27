"""Pedidos de verificacao de integridade: a tela grava o pedido e o worker verifica.

A verificacao refaz todos os calculos, rele todos os arquivos do S3 e confere a trilha
inteira; o tempo cresce com o uso e nao cabe numa requisicao. A tela so enfileira; o worker
roda servico.verificar com a conexao asyncpg do tenant e marca o desfecho no pedido. A
verificacao diaria do beat continua direto (nao passa por pedido).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import structlog

from app.core import audit
from app.core.armazenamento import ArmazemObjetos
from app.core.erros import AcessoNegado, ErroValidacao
from app.modules.integridade import servico

log = structlog.get_logger(__name__)

# Pedido que o worker nao pegou nesse tempo (worker parado, fila perdida) aparece como falhou
# na leitura e devolve o botao; o proximo pedido grava a falha de verdade. O prazo e maior que
# o do relatorio: num cliente grande a verificacao leva minutos.
PRAZO_DA_FILA_MIN = 60
MENSAGEM_PRAZO = (
    "A verificação não foi processada a tempo e saiu da fila. Nada foi verificado; peça de novo."
)
MENSAGEM_FALHA_INESPERADA = (
    "A verificação parou por um erro interno e não foi registrada. Tente de novo; se repetir,"
    " avise o suporte com a hora do pedido."
)


class PedidoNaFila(ErroValidacao):
    def __init__(self) -> None:
        super().__init__(
            "verificacao",
            "Já há uma verificação em andamento. Aguarde: a tela se atualiza sozinha quando"
            " terminar.",
        )


@dataclass(frozen=True)
class Pedido:
    pedido_id: UUID
    situacao: str
    pedido_em: datetime
    concluido_em: datetime | None
    mensagem: str | None
    verificacao_id: UUID | None


async def pedir(conexao: Any, *, tenant_id: UUID, usuario_id: UUID) -> UUID:
    """Grava o pedido na fila. Um so por tenant enquanto estiver na fila."""
    pedido_id = uuid4()
    await conexao.execute(
        "UPDATE integridade_pedido SET situacao = 'falhou', mensagem = $1, concluido_em = now()"
        " WHERE situacao = 'na_fila' AND pedido_em < now() - make_interval(mins => $2)",
        MENSAGEM_PRAZO,
        PRAZO_DA_FILA_MIN,
    )
    # Sem savepoint: na API a transacao e da sessao SQLAlchemy; a recusa desfaz tudo mesmo.
    try:
        await conexao.execute(
            "INSERT INTO integridade_pedido (tenant_id, pedido_id, pedido_por) VALUES ($1, $2, $3)",
            tenant_id,
            pedido_id,
            usuario_id,
        )
    except asyncpg.UniqueViolationError as erro:
        raise PedidoNaFila from erro
    await audit.registrar_asyncpg(
        conexao,
        tenant_id=tenant_id,
        usuario_id=usuario_id,
        acao="integridade.pedida",
        entidade="integridade_pedido",
        entidade_id=pedido_id,
    )
    return pedido_id


async def _concluir(
    conexao: Any, pedido_id: UUID, verificacao_id: UUID | None, mensagem: str | None
) -> None:
    await conexao.execute(
        "UPDATE integridade_pedido SET situacao = $2, verificacao_id = $3, mensagem = $4,"
        " concluido_em = now() WHERE pedido_id = $1 AND situacao = 'na_fila'",
        pedido_id,
        "concluido" if verificacao_id is not None else "falhou",
        verificacao_id,
        mensagem,
    )


async def processar(
    conexao: Any, *, tenant_id: UUID, pedido_id: UUID, armazem: ArmazemObjetos
) -> str:
    """Verifica e marca o desfecho. Devolve a situacao final.

    Pedido que ja saiu da fila (tarefa repetida ou preso alem do prazo) nao verifica de novo.
    Falha nao derruba a transacao: a verificacao roda num savepoint e o pedido sai como falhou.
    Falha de integridade encontrada nao e falha do pedido: a verificacao e registrada com ela.
    """
    linha = await conexao.fetchrow(
        "SELECT pedido_por, situacao,"
        " situacao = 'na_fila' AND pedido_em < now() - make_interval(mins => $2) AS preso"
        " FROM integridade_pedido WHERE pedido_id = $1 FOR UPDATE",
        pedido_id,
        PRAZO_DA_FILA_MIN,
    )
    if linha is None:
        raise AcessoNegado
    if linha["preso"]:
        await _concluir(conexao, pedido_id, None, MENSAGEM_PRAZO)
        return "falhou"
    if linha["situacao"] != "na_fila":
        return str(linha["situacao"])
    try:
        async with conexao.transaction():
            relatorio = await servico.verificar(
                conexao, tenant_id, armazem, origem="manual", pedida_por=linha["pedido_por"]
            )
    except Exception:
        # Sem dado pessoal no log: so os ids. A pessoa ve a mensagem generica na tela.
        log.exception("integridade.pedido_falhou", pedido_id=str(pedido_id))
        await _concluir(conexao, pedido_id, None, MENSAGEM_FALHA_INESPERADA)
        return "falhou"
    await _concluir(conexao, pedido_id, relatorio.verificacao_id, None)
    return "concluido"


_PRESO = (
    f"situacao = 'na_fila' AND pedido_em < now() - make_interval(mins => {int(PRAZO_DA_FILA_MIN)})"
)
# Com o prazo aplicado: na fila alem do prazo volta como falhou, sem escrever na leitura. A
# mensagem do prazo vai como parametro, nunca montada no SQL.
_COLUNAS = (
    "pedido_id,"
    f" CASE WHEN {_PRESO} THEN 'falhou' ELSE situacao END AS situacao,"
    " pedido_em, concluido_em,"
    f" CASE WHEN {_PRESO} THEN $1::text ELSE mensagem END AS mensagem,"
    " verificacao_id"
)


async def obter(conexao: Any, pedido_id: UUID) -> Pedido:
    linha = await conexao.fetchrow(
        f"SELECT {_COLUNAS} FROM integridade_pedido WHERE pedido_id = $2",  # noqa: S608  # nosec B608
        MENSAGEM_PRAZO,
        pedido_id,
    )
    if linha is None:
        raise AcessoNegado
    return Pedido(**dict(linha))


async def ultimo(conexao: Any) -> Pedido | None:
    """O pedido na fila (ha no maximo um), ou o mais recente."""
    linha = await conexao.fetchrow(
        f"SELECT {_COLUNAS} FROM integridade_pedido"  # noqa: S608  # nosec B608
        " ORDER BY situacao = 'na_fila' DESC, pedido_em DESC LIMIT 1",
        MENSAGEM_PRAZO,
    )
    return None if linha is None else Pedido(**dict(linha))
