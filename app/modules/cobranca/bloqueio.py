"""Bloqueio por inadimplencia (decisao do usuario: so acoes novas).

Com a assinatura em "pagamento em atraso" (fatura vencida alem da tolerancia), ficam
bloqueados abrir inventario, fechar inventario, gerar relatorio assinado, convidar pessoas e
dar acesso novo a verificador (decisao do usuario em 2026-11-26). Continuam: lancar e aprovar
dados, consultar, baixar relatorios, o verificador que ja tem acesso, revogar acesso, a
cobranca e a LGPD. Sem cobranca configurada (COBRANCA_TABELA), nunca bloqueia.
"""

from typing import Any

from sqlalchemy import text

from app.core.config import obter_configuracao
from app.core.erros import ErroValidacao

MENSAGEM = (
    "Pagamento em atraso: abrir inventário, fechar, gerar relatório, convidar pessoas e dar"
    " acesso a verificador ficam bloqueados até a fatura vencida ser paga. O administrador"
    " regulariza na tela Cobrança."
)


async def em_atraso(sessao: Any) -> bool:
    """A assinatura do tenant do contexto esta inadimplente (e a cobranca esta ligada)?"""
    if not obter_configuracao().cobranca_tabela:
        return False
    situacao = await sessao.scalar(text("SELECT situacao FROM assinatura"))
    return bool(situacao == "inadimplente")


async def exigir_em_dia(sessao: Any) -> None:
    if await em_atraso(sessao):
        raise ErroValidacao("cobranca", MENSAGEM)
