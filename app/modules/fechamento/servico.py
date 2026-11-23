"""Fechamento e reapresentacao do inventario (asyncpg, na transacao da requisicao).

Fechar exige, tudo conferido na mesma transacao, com o inventario travado (FOR UPDATE):
1. inventario em coleta;
2. um calculo feito;
3. calculo atualizado: a entrada montada agora, com o mesmo instante de conhecimento, tem o
   mesmo hash da entrada gravada (nenhum dado, aprovacao ou fonte mudou desde o calculo);
4. calculo sem pendencias;
5. a conferencia refaz o calculo e chega aos mesmos hashes;
6. a pessoa digitou o nome da empresa.
Periodo sem evidencia vira aviso, nao bloqueio.

O fechamento grava um evento so de insercao com a versao (1, 2 depois da primeira
reapresentacao...), o calculo usado e o hash do fechamento. Reabrir (reapresentacao) exige
motivo; o fechamento anterior continua guardado, como tudo o que foi lancado.
O banco garante o resto (migracao 0009): estado so muda com o evento na mesma transacao, e
inventario fechado nao recebe nada.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from app.core import audit
from app.core.erros import AcessoNegado, ErroValidacao
from app.modules.calculo import fator_direto
from app.modules.calculo import servico as calculo
from app.modules.calculo.fator_direto import hash_canonico
from app.modules.calculo.montagem import periodos

TAMANHO_MINIMO_MOTIVO = 20
TAMANHO_MAXIMO_MOTIVO = 1000

_SQL_INVENTARIO = (
    "SELECT inventario_id, empresa_id, ano, estado FROM inventario WHERE inventario_id = $1"
)
# Trava a linha ate o fim da transacao: duas pessoas nao fecham (ou reabrem) ao mesmo tempo.
_SQL_INVENTARIO_TRAVADO = _SQL_INVENTARIO + " FOR UPDATE"


@dataclass(frozen=True)
class Checagem:
    nome: str
    ok: bool
    detalhe: str


@dataclass(frozen=True)
class Evento:
    tipo: str
    versao: int
    calculo_id: UUID | None
    hash_fechamento: bytes | None
    motivo: str | None
    registrado_em: datetime


@dataclass
class Situacao:
    inventario_id: UUID
    empresa_id: UUID
    ano: int
    estado: str
    calculo_id: UUID | None
    checagens: list[Checagem] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    eventos: list[Evento] = field(default_factory=list)

    @property
    def pode_fechar(self) -> bool:
        return self.estado == "em_coleta" and all(c.ok for c in self.checagens)

    @property
    def proxima_versao(self) -> int:
        return 1 + sum(1 for e in self.eventos if e.tipo == "fechamento")

    @property
    def fechamento_atual(self) -> Evento | None:
        fechamentos = [e for e in self.eventos if e.tipo == "fechamento"]
        return fechamentos[-1] if self.estado == "fechado" and fechamentos else None


async def _inventario(conexao: Any, inventario_id: UUID, *, travar: bool = False) -> Any:
    sql = _SQL_INVENTARIO_TRAVADO if travar else _SQL_INVENTARIO
    linha = await conexao.fetchrow(sql, inventario_id)
    if linha is None:
        raise AcessoNegado
    return linha


async def _eventos(conexao: Any, inventario_id: UUID) -> list[Evento]:
    return [
        Evento(
            linha["tipo"],
            linha["versao"],
            linha["calculo_id"],
            None if linha["hash_fechamento"] is None else bytes(linha["hash_fechamento"]),
            linha["motivo"],
            linha["registrado_em"],
        )
        for linha in await conexao.fetch(
            "SELECT tipo, versao, calculo_id, hash_fechamento, motivo, registrado_em"
            " FROM inventario_evento WHERE inventario_id = $1 ORDER BY registrado_em, tipo",
            inventario_id,
        )
    ]


async def _periodos_sem_evidencia(conexao: Any, inventario_id: UUID) -> int:
    fontes = await conexao.fetch(
        "SELECT f.fonte_id, f.periodicidade,"
        " array_agg(DISTINCT e.periodo) FILTER (WHERE e.evidencia_id IS NOT NULL) AS periodos,"
        " bool_or(e.evidencia_id IS NOT NULL AND e.periodo IS NULL) AS todos"
        " FROM fonte_emissao f LEFT JOIN evidencia e"
        "   ON e.tenant_id = f.tenant_id AND e.fonte_id = f.fonte_id"
        " WHERE f.inventario_id = $1 AND f.ativa AND f.material"
        " GROUP BY f.fonte_id, f.periodicidade",
        inventario_id,
    )
    faltando = 0
    for fonte in fontes:
        if fonte["todos"]:
            continue
        cobertos = set(fonte["periodos"] or [])
        faltando += sum(1 for p in periodos(fonte["periodicidade"]) if p not in cobertos)
    return faltando


async def situacao(conexao: Any, inventario_id: UUID, *, travar: bool = False) -> Situacao:
    inventario = await _inventario(conexao, inventario_id, travar=travar)
    ultimo = await conexao.fetchrow(
        "SELECT calculo_id, conhecido_em, hash_entrada, pendencias FROM calculo"
        " WHERE inventario_id = $1 ORDER BY calculado_em DESC, calculo_id LIMIT 1",
        inventario_id,
    )
    resultado = Situacao(
        inventario_id=inventario_id,
        empresa_id=inventario["empresa_id"],
        ano=inventario["ano"],
        estado=inventario["estado"],
        calculo_id=None if ultimo is None else ultimo["calculo_id"],
        eventos=await _eventos(conexao, inventario_id),
    )
    if inventario["estado"] != "em_coleta":
        return resultado
    checagens = resultado.checagens
    if ultimo is None:
        checagens.append(Checagem("calculo", False, "Calcule as emissões em Resultados."))
        return resultado
    checagens.append(Checagem("calculo", True, "Há um cálculo feito."))
    _inv, entrada = await calculo.montar_entrada(conexao, inventario_id, ultimo["conhecido_em"])
    atualizado = fator_direto.calcular(entrada).hash_entrada == bytes(ultimo["hash_entrada"])
    checagens.append(
        Checagem(
            "atualizado",
            atualizado,
            "O cálculo usa os dados de agora."
            if atualizado
            else "Algum dado, aprovação ou fonte mudou depois do cálculo. Calcule de novo.",
        )
    )
    sem_pendencias = ultimo["pendencias"] == 0
    checagens.append(
        Checagem(
            "pendencias",
            sem_pendencias,
            "O cálculo não tem pendências."
            if sem_pendencias
            else f"O cálculo tem {ultimo['pendencias']} pendência(s). Resolva e calcule de novo.",
        )
    )
    conferencia = await calculo.refazer(conexao, ultimo["calculo_id"])
    checagens.append(
        Checagem(
            "conferido",
            conferencia.ok,
            "Refazendo a conta, o resultado e os hashes são os mesmos."
            if conferencia.ok
            else "Refazendo a conta, o resultado não confere. Avise o suporte.",
        )
    )
    sem_evidencia = await _periodos_sem_evidencia(conexao, inventario_id)
    if sem_evidencia:
        resultado.avisos.append(
            f"{sem_evidencia} período(s) sem evidência. O verificador vai pedir os documentos."
        )
    return resultado


async def hash_do_fechamento(
    conexao: Any, inventario_id: UUID, versao: int, calculo_id: UUID
) -> bytes:
    """Amarra o fechamento ao calculo (hash do resultado), aos lancamentos e as evidencias."""
    hash_resultado = await conexao.fetchval(
        "SELECT hash_resultado FROM calculo WHERE calculo_id = $1", calculo_id
    )
    lancamentos = [
        str(linha["lancamento_id"])
        for linha in await conexao.fetch(
            "SELECT DISTINCT lancamento_id FROM calculo_linha WHERE calculo_id = $1"
            " ORDER BY lancamento_id",
            calculo_id,
        )
    ]
    evidencias = [
        [str(linha["evidencia_id"]), bytes(linha["sha256"]).hex()]
        for linha in await conexao.fetch(
            "SELECT e.evidencia_id, e.sha256 FROM evidencia e JOIN fonte_emissao f"
            " ON f.tenant_id = e.tenant_id AND f.fonte_id = e.fonte_id"
            " WHERE f.inventario_id = $1 ORDER BY e.evidencia_id",
            inventario_id,
        )
    ]
    return hash_canonico(
        "fechamento",
        "v1",
        str(inventario_id),
        versao,
        str(calculo_id),
        bytes(hash_resultado).hex(),
        lancamentos,
        evidencias,
    )


async def fechar(
    conexao: Any,
    *,
    tenant_id: UUID,
    usuario_id: UUID,
    inventario_id: UUID,
    confirmacao: str,
    nome_empresa: str,
) -> Evento:
    if confirmacao.strip() != nome_empresa.strip():
        raise ErroValidacao(
            "confirmacao", f"Para fechar, digite exatamente o nome da empresa: {nome_empresa}."
        )
    atual = await situacao(conexao, inventario_id, travar=True)
    if atual.estado != "em_coleta":
        raise ErroValidacao("confirmacao", "O inventário já está fechado.")
    if not atual.pode_fechar or atual.calculo_id is None:
        faltam = "; ".join(c.detalhe for c in atual.checagens if not c.ok)
        raise ErroValidacao("confirmacao", f"Ainda não dá para fechar: {faltam}")
    versao = atual.proxima_versao
    hash_fechamento = await hash_do_fechamento(conexao, inventario_id, versao, atual.calculo_id)
    registrado_em = await conexao.fetchval(
        "INSERT INTO inventario_evento (tenant_id, evento_id, empresa_id, inventario_id, tipo,"
        " versao, calculo_id, hash_fechamento, registrado_por)"
        " VALUES ($1, $2, $3, $4, 'fechamento', $5, $6, $7, $8) RETURNING registrado_em",
        tenant_id,
        uuid4(),
        atual.empresa_id,
        inventario_id,
        versao,
        atual.calculo_id,
        hash_fechamento,
        usuario_id,
    )
    await conexao.execute(
        "UPDATE inventario SET estado = 'fechado', atualizado_em = now() WHERE inventario_id = $1",
        inventario_id,
    )
    await audit.registrar_asyncpg(
        conexao,
        tenant_id=tenant_id,
        usuario_id=usuario_id,
        acao="inventario.fechado",
        entidade="inventario",
        entidade_id=inventario_id,
        dados={
            "empresa_id": str(atual.empresa_id),
            "versao": versao,
            "calculo_id": str(atual.calculo_id),
            "hash_fechamento": hash_fechamento.hex(),
        },
    )
    return Evento("fechamento", versao, atual.calculo_id, hash_fechamento, None, registrado_em)


async def reabrir(
    conexao: Any, *, tenant_id: UUID, usuario_id: UUID, inventario_id: UUID, motivo: str
) -> None:
    limpo = motivo.strip()
    if not TAMANHO_MINIMO_MOTIVO <= len(limpo) <= TAMANHO_MAXIMO_MOTIVO:
        raise ErroValidacao(
            "motivo",
            f"Explique a reapresentação em {TAMANHO_MINIMO_MOTIVO} a {TAMANHO_MAXIMO_MOTIVO}"
            " caracteres: o que mudou e por quê. O verificador vai ler.",
        )
    atual = await situacao(conexao, inventario_id, travar=True)
    if atual.estado != "fechado":
        raise ErroValidacao("motivo", "Só um inventário fechado pode ser reapresentado.")
    versao = sum(1 for e in atual.eventos if e.tipo == "reabertura") + 1
    await conexao.execute(
        "INSERT INTO inventario_evento (tenant_id, evento_id, empresa_id, inventario_id, tipo,"
        " versao, motivo, registrado_por) VALUES ($1, $2, $3, $4, 'reabertura', $5, $6, $7)",
        tenant_id,
        uuid4(),
        atual.empresa_id,
        inventario_id,
        versao,
        limpo,
        usuario_id,
    )
    await conexao.execute(
        "UPDATE inventario SET estado = 'em_coleta', atualizado_em = now()"
        " WHERE inventario_id = $1",
        inventario_id,
    )
    await audit.registrar_asyncpg(
        conexao,
        tenant_id=tenant_id,
        usuario_id=usuario_id,
        acao="inventario.reaberto",
        entidade="inventario",
        entidade_id=inventario_id,
        dados={"empresa_id": str(atual.empresa_id), "reapresentacao": versao},
    )
