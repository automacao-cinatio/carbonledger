"""Metas de reducao e acompanhamento (sessao de tenant, com RLS).

Trajetoria linear do ano-base ao ano-alvo: o esperado de cada ano cai em partes iguais ate
base x (1 - reducao). O realizado de cada ano vem do inventario daquele ano: o calculo do
fechamento quando fechado; senao, o ultimo calculo, marcado como preliminar. As acoes sao
estimativas do cliente: aparecem ao lado, nunca entram no inventario.

Nenhum percentual, ano ou valor de referencia e sugerido pelo sistema.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.erros import AcessoNegado, ColetorDeErros, ErroValidacao
from app.core.security.acesso import Acesso
from app.web.formatos import NumeroInvalido, ler_numero

ESCOPOS = {"1": "Escopo 1", "2": "Escopo 2", "1+2": "Escopos 1 e 2"}
SITUACOES_ACAO = {
    "planejada": "Planejada",
    "em_andamento": "Em andamento",
    "concluida": "Concluída",
    "cancelada": "Cancelada",
}
CEM = Decimal(100)


@dataclass(frozen=True)
class Meta:
    meta_id: UUID
    empresa_id: UUID
    nome: str
    escopos: str
    inventario_base_id: UUID
    calculo_base_id: UUID
    ano_base: int
    valor_base_tco2e: Decimal
    ano_alvo: int
    reducao_percentual: Decimal
    ativa: bool

    @property
    def valor_alvo_tco2e(self) -> Decimal:
        return self.valor_base_tco2e * (CEM - self.reducao_percentual) / CEM

    def esperado(self, ano: int) -> Decimal:
        if ano <= self.ano_base:
            return self.valor_base_tco2e
        if ano >= self.ano_alvo:
            return self.valor_alvo_tco2e
        fracao = Decimal(ano - self.ano_base) / Decimal(self.ano_alvo - self.ano_base)
        return self.valor_base_tco2e - (self.valor_base_tco2e - self.valor_alvo_tco2e) * fracao


@dataclass(frozen=True)
class Ponto:
    ano: int
    esperado: Decimal
    realizado: Decimal | None
    preliminar: bool

    @property
    def no_caminho(self) -> bool | None:
        return None if self.realizado is None else self.realizado <= self.esperado


@dataclass(frozen=True)
class Acao:
    acao_id: UUID
    titulo: str
    descricao: str | None
    reducao_estimada_tco2e: Decimal
    ano_inicio: int
    situacao: str


def total_dos_escopos(escopos: str, escopo1: Decimal, escopo2: Decimal) -> Decimal:
    if escopos == "1":
        return escopo1
    if escopos == "2":
        return escopo2
    return escopo1 + escopo2


def _texto(valor: str | None) -> str:
    return (valor or "").strip()


def _ano(coletor: ColetorDeErros, campo: str, valor: str) -> int | None:
    limpo = _texto(valor)
    if not (limpo.isdigit() and len(limpo) == 4):
        coletor.adicionar(campo, "Informe o ano com quatro dígitos, como 2030.")
        return None
    return int(limpo)


async def inventarios_fechados(sessao: AsyncSession, empresa_id: UUID) -> list[Any]:
    """Inventarios fechados com o calculo do fechamento atual (a base possivel de uma meta)."""
    linhas = await sessao.execute(
        text(
            "SELECT DISTINCT ON (i.inventario_id) i.inventario_id, i.ano, e.calculo_id,"
            " c.escopo1_tco2e, c.escopo2_tco2e FROM inventario i"
            " JOIN inventario_evento e ON e.tenant_id = i.tenant_id"
            "  AND e.inventario_id = i.inventario_id AND e.tipo = 'fechamento'"
            " JOIN calculo c ON c.tenant_id = e.tenant_id AND c.calculo_id = e.calculo_id"
            " WHERE i.empresa_id = :empresa AND i.estado = 'fechado'"
            " ORDER BY i.inventario_id, e.versao DESC"
        ),
        {"empresa": empresa_id},
    )
    return sorted(linhas, key=lambda linha: linha.ano)


_COLUNAS_META = (
    "meta_id, empresa_id, nome, escopos, inventario_base_id, calculo_base_id, ano_base,"
    " valor_base_tco2e, ano_alvo, reducao_percentual, ativa"
)


async def listar(sessao: AsyncSession, empresa_id: UUID) -> list[Meta]:
    linhas = await sessao.execute(
        text(
            f"SELECT {_COLUNAS_META} FROM meta"  # noqa: S608  # nosec B608
            " WHERE empresa_id = :empresa ORDER BY ativa DESC, ano_alvo, nome"
        ),
        {"empresa": empresa_id},
    )
    return [Meta(**linha._asdict()) for linha in linhas]


async def obter(sessao: AsyncSession, meta_id: UUID) -> Meta:
    linha = (
        await sessao.execute(
            text(f"SELECT {_COLUNAS_META} FROM meta WHERE meta_id = :id"),  # noqa: S608  # nosec B608
            {"id": meta_id},
        )
    ).first()
    if linha is None:
        raise AcessoNegado
    return Meta(**linha._asdict())


async def criar(
    sessao: AsyncSession, acesso: Acesso, empresa_id: UUID, formulario: dict[str, str]
) -> UUID:
    coletor = ColetorDeErros()
    nome = _texto(formulario.get("nome"))
    if not nome or len(nome) > 120:
        coletor.adicionar("nome", "Dê um nome à meta, com até 120 caracteres.")
    escopos = _texto(formulario.get("escopos"))
    if escopos not in ESCOPOS:
        coletor.adicionar("escopos", "Escolha os escopos da meta.")
    bases = {str(b.inventario_id): b for b in await inventarios_fechados(sessao, empresa_id)}
    base = bases.get(_texto(formulario.get("inventario_base_id")))
    if base is None:
        coletor.adicionar("inventario_base_id", "Escolha o ano-base entre os inventários fechados.")
    ano_alvo = _ano(coletor, "ano_alvo", formulario.get("ano_alvo", ""))
    if ano_alvo is not None and base is not None and ano_alvo <= base.ano:
        coletor.adicionar("ano_alvo", f"O ano-alvo precisa ser depois do ano-base ({base.ano}).")
    reducao = None
    try:
        reducao = ler_numero(_texto(formulario.get("reducao_percentual")))
        if not Decimal(0) < reducao <= CEM:
            coletor.adicionar("reducao_percentual", "A redução fica entre 0 e 100%.")
    except NumeroInvalido:
        coletor.adicionar(
            "reducao_percentual", "Informe a redução em %, com vírgula para os decimais."
        )
    coletor.levantar_se_houver()
    if base is None or ano_alvo is None or reducao is None:  # ja recusado; so para o tipo
        raise ErroValidacao("nome", "Confira os campos.")
    valor_base = total_dos_escopos(escopos, base.escopo1_tco2e, base.escopo2_tco2e)
    meta_id = uuid4()
    await sessao.execute(
        text(
            "INSERT INTO meta (tenant_id, meta_id, empresa_id, nome, escopos, inventario_base_id,"
            " calculo_base_id, ano_base, valor_base_tco2e, ano_alvo, reducao_percentual,"
            " criado_por) VALUES (:tenant, :id, :empresa, :nome, :escopos, :inventario,"
            " :calculo, :ano_base, :valor, :ano_alvo, :reducao, :usuario)"
        ),
        {
            "tenant": acesso.tenant_id,
            "id": meta_id,
            "empresa": empresa_id,
            "nome": nome,
            "escopos": escopos,
            "inventario": base.inventario_id,
            "calculo": base.calculo_id,
            "ano_base": base.ano,
            "valor": valor_base,
            "ano_alvo": ano_alvo,
            "reducao": reducao,
            "usuario": acesso.usuario_id,
        },
    )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="meta.criada",
        entidade="meta",
        entidade_id=meta_id,
        dados={"empresa_id": str(empresa_id), "escopos": escopos, "ano_alvo": ano_alvo},
    )
    return meta_id


async def definir_ativa(
    sessao: AsyncSession, acesso: Acesso, meta_id: UUID, *, ativa: bool
) -> Meta:
    meta = await obter(sessao, meta_id)
    await sessao.execute(
        text("UPDATE meta SET ativa = :ativa, atualizado_em = now() WHERE meta_id = :id"),
        {"id": meta_id, "ativa": ativa},
    )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="meta.reativada" if ativa else "meta.arquivada",
        entidade="meta",
        entidade_id=meta_id,
        dados={"empresa_id": str(meta.empresa_id)},
    )
    return meta


async def acompanhamento(sessao: AsyncSession, meta: Meta) -> list[Ponto]:
    """Um ponto por ano, do ano-base ao ano-alvo, com o realizado onde ha calculo."""
    linhas = await sessao.execute(
        text(
            "SELECT i.ano, i.estado, c.escopo1_tco2e AS escopo1, c.escopo2_tco2e AS escopo2"
            " FROM inventario i"
            " LEFT JOIN LATERAL (SELECT coalesce("
            "   (SELECT e.calculo_id FROM inventario_evento e"
            "     WHERE e.tenant_id = i.tenant_id AND e.inventario_id = i.inventario_id"
            "       AND e.tipo = 'fechamento' ORDER BY e.versao DESC LIMIT 1),"
            "   (SELECT c2.calculo_id FROM calculo c2"
            "     WHERE c2.tenant_id = i.tenant_id AND c2.inventario_id = i.inventario_id"
            "     ORDER BY c2.calculado_em DESC LIMIT 1)) AS calculo_id) x ON true"
            " LEFT JOIN calculo c ON c.tenant_id = i.tenant_id AND c.calculo_id = x.calculo_id"
            " WHERE i.empresa_id = :empresa"
            " AND i.ano BETWEEN :inicio AND :fim"
        ),
        {"empresa": meta.empresa_id, "inicio": meta.ano_base, "fim": meta.ano_alvo},
    )
    realizados = {
        linha.ano: (
            total_dos_escopos(meta.escopos, linha.escopo1, linha.escopo2),
            linha.estado != "fechado",
        )
        for linha in linhas
        if linha.escopo1 is not None
    }
    return [
        Ponto(
            ano,
            meta.esperado(ano),
            realizados[ano][0] if ano in realizados else None,
            realizados[ano][1] if ano in realizados else False,
        )
        for ano in range(meta.ano_base, meta.ano_alvo + 1)
    ]


# ---------------------------------------------------------------- acoes


async def listar_acoes(sessao: AsyncSession, meta_id: UUID) -> list[Acao]:
    linhas = await sessao.execute(
        text(
            "SELECT acao_id, titulo, descricao, reducao_estimada_tco2e, ano_inicio, situacao"
            " FROM acao_reducao WHERE meta_id = :meta ORDER BY ano_inicio, titulo"
        ),
        {"meta": meta_id},
    )
    return [Acao(**linha._asdict()) for linha in linhas]


async def criar_acao(
    sessao: AsyncSession, acesso: Acesso, meta_id: UUID, formulario: dict[str, str]
) -> UUID:
    meta = await obter(sessao, meta_id)
    coletor = ColetorDeErros()
    titulo = _texto(formulario.get("titulo"))
    if not titulo or len(titulo) > 120:
        coletor.adicionar("titulo", "Dê um título à ação, com até 120 caracteres.")
    descricao = _texto(formulario.get("descricao")) or None
    if descricao and len(descricao) > 500:
        coletor.adicionar("descricao", "Use no máximo 500 caracteres.")
    estimativa = None
    try:
        estimativa = ler_numero(_texto(formulario.get("reducao_estimada_tco2e")))
        if estimativa < 0:
            coletor.adicionar("reducao_estimada_tco2e", "A redução estimada não pode ser negativa.")
    except NumeroInvalido:
        coletor.adicionar(
            "reducao_estimada_tco2e", "Informe a redução estimada em tCO₂e por ano, como 12,5."
        )
    ano = _ano(coletor, "ano_inicio", formulario.get("ano_inicio", ""))
    coletor.levantar_se_houver()
    if estimativa is None or ano is None:  # ja recusado; so para o tipo
        raise ErroValidacao("titulo", "Confira os campos.")
    acao_id = uuid4()
    await sessao.execute(
        text(
            "INSERT INTO acao_reducao (tenant_id, acao_id, empresa_id, meta_id, titulo, descricao,"
            " reducao_estimada_tco2e, ano_inicio, criado_por) VALUES (:tenant, :id, :empresa,"
            " :meta, :titulo, :descricao, :estimativa, :ano, :usuario)"
        ),
        {
            "tenant": acesso.tenant_id,
            "id": acao_id,
            "empresa": meta.empresa_id,
            "meta": meta_id,
            "titulo": titulo,
            "descricao": descricao,
            "estimativa": estimativa,
            "ano": ano,
            "usuario": acesso.usuario_id,
        },
    )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="acao.criada",
        entidade="acao_reducao",
        entidade_id=acao_id,
        dados={"empresa_id": str(meta.empresa_id), "meta_id": str(meta_id)},
    )
    return acao_id


async def mudar_situacao(
    sessao: AsyncSession, acesso: Acesso, acao_id: UUID, situacao: str
) -> UUID:
    if situacao not in SITUACOES_ACAO:
        raise ErroValidacao("situacao", "Escolha a situação da ação.")
    resultado = await sessao.execute(
        text(
            "UPDATE acao_reducao SET situacao = :situacao, atualizado_em = now()"
            " WHERE acao_id = :id RETURNING meta_id, empresa_id"
        ),
        {"id": acao_id, "situacao": situacao},
    )
    linha = resultado.first()
    if linha is None:
        raise AcessoNegado
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="acao.alterada",
        entidade="acao_reducao",
        entidade_id=acao_id,
        dados={"empresa_id": str(linha.empresa_id), "situacao": situacao},
    )
    return UUID(str(linha.meta_id))
