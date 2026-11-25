"""Pedidos de relatorio: a tela grava o pedido e o worker gera, assina e guarda.

A geracao (Chromium, assinatura e, com TSA, a ida ao carimbo do tempo) leva segundos e
dependia de servicos externos dentro da requisicao. Agora a tela so valida e enfileira; o
worker processa com a conexao asyncpg do tenant e marca o desfecho no proprio pedido.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import structlog
from pyhanko.sign import timestamps

from app.core import audit, keys
from app.core.armazenamento import ArmazemObjetos
from app.core.erros import AcessoNegado, ErroValidacao
from app.modules.relatorios import certificados, relatorio

log = structlog.get_logger(__name__)

# Pedido que o worker nao pegou nesse tempo (worker parado, fila perdida, Redis fora no
# momento de enfileirar) ja aparece como falhou na leitura, o que devolve o botao de gerar; o
# proximo pedido grava a falha de verdade. Se o worker pegar depois, nao gera.
PRAZO_DA_FILA_MIN = 15
MENSAGEM_PRAZO = (
    "O pedido não foi processado a tempo e saiu da fila. Nada foi gerado; peça de novo."
)
MENSAGEM_SEM_CHAVE = (
    "A plataforma está sem a chave que abre o certificado da empresa. Nada foi gerado; avise o"
    " suporte."
)
# Sem "nada foi gravado": o PDF pode ja ter ido para o armazenamento (com Object Lock) antes
# da falha; o que se garante e que nenhum relatorio foi registrado.
MENSAGEM_FALHA_INESPERADA = (
    "A geração falhou por um erro interno e o relatório não foi registrado. Tente de novo; se"
    " repetir, avise o suporte com a hora do pedido."
)


class PedidoNaFila(ErroValidacao):
    def __init__(self) -> None:
        super().__init__(
            "relatorio",
            "Já há um relatório deste inventário sendo gerado. Aguarde: a tela se atualiza"
            " sozinha quando ficar pronto.",
        )


@dataclass(frozen=True)
class Pedido:
    pedido_id: UUID
    inventario_id: UUID
    situacao: str
    pedido_em: datetime
    concluido_em: datetime | None
    mensagem: str | None
    relatorio_id: UUID | None


async def pedir(
    conexao: Any,
    *,
    tenant_id: UUID,
    usuario_id: UUID,
    inventario_id: UUID,
    empresa_id: UUID,
    certificado_id: UUID,
) -> UUID:
    """Grava o pedido na fila. Um so por inventario enquanto estiver na fila."""
    pedido_id = uuid4()
    await conexao.execute(
        "UPDATE relatorio_pedido SET situacao = 'falhou', mensagem = $2, concluido_em = now()"
        " WHERE inventario_id = $1 AND situacao = 'na_fila'"
        " AND pedido_em < now() - make_interval(mins => $3)",
        inventario_id,
        MENSAGEM_PRAZO,
        PRAZO_DA_FILA_MIN,
    )
    # Sem savepoint: na API a transacao e da sessao SQLAlchemy; a recusa desfaz tudo mesmo.
    try:
        await conexao.execute(
            "INSERT INTO relatorio_pedido (tenant_id, pedido_id, empresa_id, inventario_id,"
            " certificado_id, pedido_por) VALUES ($1, $2, $3, $4, $5, $6)",
            tenant_id,
            pedido_id,
            empresa_id,
            inventario_id,
            certificado_id,
            usuario_id,
        )
    except asyncpg.UniqueViolationError as erro:
        raise PedidoNaFila from erro
    await audit.registrar_asyncpg(
        conexao,
        tenant_id=tenant_id,
        usuario_id=usuario_id,
        acao="relatorio.pedido",
        entidade="relatorio_pedido",
        entidade_id=pedido_id,
        dados={"inventario_id": str(inventario_id), "certificado_id": str(certificado_id)},
    )
    return pedido_id


async def _concluir(
    conexao: Any, pedido_id: UUID, relatorio_id: UUID | None, mensagem: str | None
) -> None:
    await conexao.execute(
        "UPDATE relatorio_pedido SET situacao = $2, relatorio_id = $3, mensagem = $4,"
        " concluido_em = now() WHERE pedido_id = $1 AND situacao = 'na_fila'",
        pedido_id,
        "pronto" if relatorio_id is not None else "falhou",
        relatorio_id,
        mensagem,
    )


async def processar(
    conexao: Any,
    *,
    tenant_id: UUID,
    pedido_id: UUID,
    provedor: keys.ProvedorChaves | None,
    armazem: ArmazemObjetos,
    carimbadora: timestamps.TimeStamper | None,
) -> str:
    """Gera o relatorio do pedido e marca o desfecho. Devolve a situacao final.

    Pedido que ja saiu da fila (tarefa repetida) nao gera de novo. Falha nao derruba a
    transacao: a geracao roda num savepoint e o pedido sai como falhou, com a mensagem.
    """
    linha = await conexao.fetchrow(
        "SELECT inventario_id, certificado_id, pedido_por, situacao FROM relatorio_pedido"
        " WHERE pedido_id = $1 FOR UPDATE",
        pedido_id,
    )
    if linha is None:
        raise AcessoNegado
    if linha["situacao"] != "na_fila":
        return str(linha["situacao"])
    if provedor is None:
        await _concluir(conexao, pedido_id, None, MENSAGEM_SEM_CHAVE)
        return "falhou"
    try:
        async with conexao.transaction():
            certificado = await certificados.obter_asyncpg(conexao, linha["certificado_id"])
            relatorio_id = await relatorio.gerar(
                conexao,
                tenant_id=tenant_id,
                usuario_id=linha["pedido_por"],
                inventario_id=linha["inventario_id"],
                certificado=certificado,
                provedor=provedor,
                armazem=armazem,
                carimbadora=carimbadora,
            )
    except ErroValidacao as erro:
        await _concluir(conexao, pedido_id, None, erro.mensagem)
        return "falhou"
    except Exception:
        # Sem dado pessoal no log: so os ids. A pessoa ve a mensagem generica na tela.
        log.exception("relatorio.pedido_falhou", pedido_id=str(pedido_id))
        await _concluir(conexao, pedido_id, None, MENSAGEM_FALHA_INESPERADA)
        return "falhou"
    await _concluir(conexao, pedido_id, relatorio_id, None)
    return "pronto"


_PRESO = (
    f"situacao = 'na_fila' AND pedido_em < now() - make_interval(mins => {int(PRAZO_DA_FILA_MIN)})"
)


def _colunas(parametro_mensagem: int) -> str:
    """Colunas com o prazo aplicado: na fila alem do prazo volta como falhou, sem escrever na
    leitura. A mensagem do prazo vai como parametro ($n), nunca montada no SQL."""
    return (
        "pedido_id, inventario_id,"
        f" CASE WHEN {_PRESO} THEN 'falhou' ELSE situacao END AS situacao,"
        " pedido_em, concluido_em,"
        f" CASE WHEN {_PRESO} THEN ${parametro_mensagem}::text ELSE mensagem END AS mensagem,"
        " relatorio_id"
    )


async def obter(conexao: Any, pedido_id: UUID) -> Pedido:
    linha = await conexao.fetchrow(
        f"SELECT {_colunas(2)} FROM relatorio_pedido WHERE pedido_id = $1",  # noqa: S608  # nosec B608
        pedido_id,
        MENSAGEM_PRAZO,
    )
    if linha is None:
        raise AcessoNegado
    return Pedido(**dict(linha))


async def do_inventario(conexao: Any, inventario_id: UUID, limite: int = 5) -> list[Pedido]:
    linhas = await conexao.fetch(
        f"SELECT {_colunas(3)} FROM relatorio_pedido WHERE inventario_id = $1"  # noqa: S608  # nosec B608
        " ORDER BY pedido_em DESC LIMIT $2",
        inventario_id,
        limite,
        MENSAGEM_PRAZO,
    )
    return [Pedido(**dict(linha)) for linha in linhas]
