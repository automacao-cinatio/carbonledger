"""Calculo dos escopos 1 e 2 no banco: monta a entrada, roda o metodo e grava a execucao.

Usa asyncpg direto (a mesma funcao serve ao worker e a API, que passa a conexao asyncpg da
propria sessao, com o contexto de tenant ja aplicado). A gravacao e so insercao.

As regras de montagem (o que entra, pendencias, data de referencia, mistura) moram em
app/modules/calculo/montagem.py, compartilhadas com a paridade. Pendencias, nunca zero:
- falta_dado: periodo sem dado, aguardando aprovacao ou devolvido;
- sem_atividade: fonte sem o codigo do combustivel ou da atividade;
- sem_fator: componente sem fator aprovado na categoria e na data;
- unidade_incompativel: grandeza do dado diferente da do fator;
- sem_gwp: gas sem GWP no conjunto do inventario.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from app.core import audit
from app.modules.calculo import fator_direto, montagem
from app.modules.calculo.fator_direto import Entrada, FatorUsado, GwpUsado, Resultado
from app.modules.calculo.montagem import (
    DadoAtual,
    FonteParaCalculo,
    ParteDaMistura,
    data_de_referencia,
)
from app.modules.fatores import repositorio
from app.modules.organizacao.limites import Abordagem

__all__ = ["data_de_referencia"]


class InventarioInexistente(LookupError):
    """Inventario fora do alcance do contexto (ou inexistente): a rota responde 404."""


@dataclass(frozen=True)
class Execucao:
    calculo_id: UUID
    resultado: Resultado


class ReferenciaDoBanco:
    """Fatores, misturas e GWP aprovados ate conhecido_em, lidos das tabelas de referencia."""

    def __init__(self, conexao: Any, conhecido_em: datetime) -> None:
        self.conexao = conexao
        self.conhecido_em = conhecido_em

    async def mistura(self, combustivel: str, quando: date) -> list[ParteDaMistura]:
        return [
            ParteDaMistura(m.componente, m.percentual, m.mistura_versao_id, m.hash_conteudo)
            for m in await repositorio.mistura_na_data(
                self.conexao, combustivel, quando, self.conhecido_em
            )
        ]

    async def fatores(self, categoria: str, codigo: str, quando: date) -> list[FatorUsado]:
        return [
            FatorUsado(f.fator_versao_id, f.gas, f.biogenico, f.valor, f.unidade, f.hash_conteudo)
            for f in await repositorio.fatores_na_categoria(
                self.conexao, categoria, codigo, quando, self.conhecido_em
            )
        ]

    async def gwp(self, conjunto: str, gas: str) -> GwpUsado | None:
        achado = await repositorio.gwp_do_gas(self.conexao, conjunto, gas, self.conhecido_em)
        if achado is None:
            return None
        return GwpUsado(achado.gwp_versao_id, gas, achado.valor, achado.hash_conteudo)


async def _inventario(conexao: Any, inventario_id: UUID) -> Any:
    linha = await conexao.fetchrow(
        "SELECT inventario_id, empresa_id, ano, abordagem, gwp_conjunto FROM inventario"
        " WHERE inventario_id = $1",
        inventario_id,
    )
    if linha is None:
        raise InventarioInexistente
    return linha


async def montar_entrada(
    conexao: Any, inventario_id: UUID, conhecido_em: datetime
) -> tuple[Any, Entrada]:
    inventario = await _inventario(conexao, inventario_id)
    fontes = [
        FonteParaCalculo(
            fonte_id=f["fonte_id"],
            categoria=f["categoria"],
            atividade=f["atividade"],
            periodicidade=f["periodicidade"],
            participacao_societaria=f["participacao_societaria"],
            controle_operacional=f["controle_operacional"],
            controle_financeiro=f["controle_financeiro"],
        )
        for f in await conexao.fetch(
            "SELECT f.fonte_id, f.categoria, f.atividade, f.periodicidade,"
            " u.participacao_societaria, u.controle_operacional, u.controle_financeiro"
            " FROM fonte_emissao f JOIN unidade u"
            "   ON u.tenant_id = f.tenant_id AND u.unidade_id = f.unidade_id"
            " WHERE f.inventario_id = $1 AND f.ativa AND f.material",
            inventario_id,
        )
    ]
    atuais = {
        (linha["fonte_id"], linha["periodo"]): DadoAtual(
            linha["lancamento_id"],
            linha["quantidade"],
            linha["unidade"],
            linha["data_compra"],
            linha["decisao"],
        )
        for linha in await conexao.fetch(
            "SELECT DISTINCT ON (l.fonte_id, l.periodo) l.lancamento_id, l.fonte_id, l.periodo,"
            " l.quantidade, l.unidade, l.data_compra, d.decisao"
            " FROM lancamento l"
            " LEFT JOIN lancamento_decisao d"
            "   ON d.tenant_id = l.tenant_id AND d.lancamento_id = l.lancamento_id"
            " WHERE l.fonte_id = ANY($1::uuid[])"
            " ORDER BY l.fonte_id, l.periodo, l.versao DESC",
            [f.fonte_id for f in fontes],
        )
    }
    entrada = await montagem.montar(
        ano=inventario["ano"],
        abordagem=Abordagem(inventario["abordagem"]),
        gwp_conjunto=inventario["gwp_conjunto"],
        conhecido_em=conhecido_em,
        fontes=fontes,
        atuais=atuais,
        referencia=ReferenciaDoBanco(conexao, conhecido_em),
    )
    return inventario, entrada


async def _gravar(
    conexao: Any,
    tenant_id: UUID,
    inventario: Any,
    entrada: Entrada,
    resultado: Resultado,
    usuario_id: UUID | None,
) -> UUID:
    calculo_id = uuid4()
    empresa_id = inventario["empresa_id"]
    await conexao.execute(
        "INSERT INTO calculo (tenant_id, calculo_id, empresa_id, inventario_id, metodo,"
        " metodo_versao, gwp_conjunto, conhecido_em, entrada, hash_entrada, hash_resultado,"
        " escopo1_tco2e, escopo2_tco2e, escopo3_tco2e, biogenico_tco2, linhas, pendencias,"
        " calculado_por) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13,"
        " $14, $15, $16, $17, $18)",
        tenant_id,
        calculo_id,
        empresa_id,
        inventario["inventario_id"],
        resultado.metodo,
        resultado.versao,
        entrada.gwp_conjunto,
        entrada.conhecido_em,
        fator_direto.entrada_para_json(entrada),
        resultado.hash_entrada,
        resultado.hash_resultado,
        resultado.escopo1_tco2e,
        resultado.escopo2_tco2e,
        resultado.escopo3_tco2e,
        resultado.biogenico_tco2,
        len(resultado.linhas),
        len(resultado.pendencias),
        usuario_id,
    )
    if resultado.linhas:
        await conexao.executemany(
            "INSERT INTO calculo_linha (tenant_id, calculo_id, linha, empresa_id, fonte_id,"
            " lancamento_id, periodo, categoria, atividade, componente, percentual_mistura,"
            " mistura_versao_id, data_referencia, quantidade, unidade, quantidade_convertida, gas,"
            " biogenico, fator, unidade_fator, fator_versao_id, gwp, gwp_versao_id,"
            " percentual_consolidado, massa_t, tco2e) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,"
            " $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25,"
            " $26)",
            [
                (
                    tenant_id,
                    calculo_id,
                    numero,
                    empresa_id,
                    linha.fonte_id,
                    linha.lancamento_id,
                    linha.periodo,
                    linha.categoria,
                    linha.atividade,
                    linha.componente,
                    linha.percentual_mistura,
                    linha.mistura_versao_id,
                    linha.data_referencia,
                    linha.quantidade,
                    linha.unidade,
                    linha.quantidade_convertida,
                    linha.gas,
                    linha.biogenico,
                    linha.fator,
                    linha.unidade_fator,
                    linha.fator_versao_id,
                    linha.gwp,
                    linha.gwp_versao_id,
                    linha.percentual_consolidado,
                    linha.massa_t,
                    linha.tco2e,
                )
                for numero, linha in enumerate(resultado.linhas, start=1)
            ],
        )
    if resultado.pendencias:
        await conexao.executemany(
            "INSERT INTO calculo_pendencia (tenant_id, calculo_id, item, empresa_id, fonte_id,"
            " periodo, motivo, detalhe) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            [
                (
                    tenant_id,
                    calculo_id,
                    numero,
                    empresa_id,
                    p.fonte_id,
                    p.periodo,
                    p.motivo,
                    p.detalhe,
                )
                for numero, p in enumerate(resultado.pendencias, start=1)
            ],
        )
    await audit.registrar_asyncpg(
        conexao,
        tenant_id=tenant_id,
        usuario_id=usuario_id,
        acao="calculo.executado",
        entidade="calculo",
        entidade_id=calculo_id,
        dados={
            "empresa_id": str(empresa_id),
            "inventario_id": str(inventario["inventario_id"]),
            "metodo": f"{resultado.metodo} {resultado.versao}",
            "hash_entrada": resultado.hash_entrada.hex(),
            "hash_resultado": resultado.hash_resultado.hex(),
            "pendencias": len(resultado.pendencias),
        },
    )
    return calculo_id


async def calcular_inventario(
    conexao: Any, tenant_id: UUID, inventario_id: UUID, usuario_id: UUID | None
) -> Execucao:
    """Calcula com a referencia conhecida agora (relogio do banco) e grava a execucao."""
    conhecido_em = await repositorio.instante_do_banco(conexao)
    inventario, entrada = await montar_entrada(conexao, inventario_id, conhecido_em)
    resultado = fator_direto.calcular(entrada)
    calculo_id = await _gravar(conexao, tenant_id, inventario, entrada, resultado, usuario_id)
    return Execucao(calculo_id, resultado)


@dataclass(frozen=True)
class Conferencia:
    entrada_integra: bool
    resultado_igual: bool

    @property
    def ok(self) -> bool:
        return self.entrada_integra and self.resultado_igual


async def refazer(conexao: Any, calculo_id: UUID) -> Conferencia:
    """Refaz a conta a partir da entrada guardada e compara os dois hashes."""
    linha = await conexao.fetchrow(
        "SELECT entrada::text AS entrada, hash_entrada, hash_resultado, metodo_versao"
        " FROM calculo WHERE calculo_id = $1",
        calculo_id,
    )
    if linha is None:
        raise InventarioInexistente
    if linha["metodo_versao"] not in fator_direto.VERSOES_COMPATIVEIS:
        raise ValueError(f"metodo {linha['metodo_versao']} nao e refeito por este codigo")
    try:
        entrada = fator_direto.entrada_de_json(linha["entrada"])
    except (KeyError, TypeError, ValueError, ArithmeticError):
        # Entrada guardada ilegivel: nao confere (e nunca derruba a tela de fechamento).
        return Conferencia(entrada_integra=False, resultado_igual=False)
    resultado = fator_direto.calcular(entrada, linha["metodo_versao"])
    return Conferencia(
        entrada_integra=resultado.hash_entrada == bytes(linha["hash_entrada"]),
        resultado_igual=resultado.hash_resultado == bytes(linha["hash_resultado"]),
    )
