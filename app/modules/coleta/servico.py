"""Inventario, fontes de emissao, lancamentos, decisoes e progresso da coleta.

Toda consulta passa pelo RLS da sessao de tenant. Quem pode o que (conferido nas rotas):
- abrir inventario: administrador e gestor;
- fontes: administrador, gestor e responsavel (nas empresas dele);
- lancar dado e enviar evidencia: todos os perfis;
- aprovar ou devolver: administrador, gestor e responsavel, nunca quem lancou.

Nada some: lancamento e decisao so recebem insercao; corrigir e gravar uma versao nova, que
volta a pedir aprovacao. O banco confere periodo, inventario em coleta, fonte ativa e a
segregacao de funcoes (migracao 0006); as checagens daqui existem para a mensagem ser clara.
Observacao, motivo e justificativa nao vao para a trilha: texto livre pode ter dado pessoal.
"""

from collections import defaultdict
from dataclasses import dataclass, field, fields
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.erros import AcessoNegado, ColetorDeErros, ErroValidacao
from app.core.security.acesso import Acesso
from app.modules.calculo.unidades import (
    UnidadeInvalida,
    mesma_dimensao,
    validar_unidade_de_atividade,
)
from app.modules.coleta.categorias import categoria, periodos
from app.modules.organizacao.limites import SQL_DENTRO_DO_LIMITE, Abordagem
from app.web.formatos import NumeroInvalido, ler_numero

GWP_PADRAO = "AR5_100"
ANO_MINIMO = 2000
TAMANHO_MAXIMO_DESCRICAO = 200
TAMANHO_MAXIMO_TEXTO = 500
MINIMO_JUSTIFICATIVA = 10
MINIMO_MOTIVO = 5
CASAS_MAXIMAS = 6
DIGITOS_INTEIROS_MAXIMOS = 15

ROTULOS_ESTADO = {"em_coleta": "Em coleta", "fechado": "Fechado"}


# ---------------------------------------------------------------- tipos


@dataclass(frozen=True)
class Inventario:
    inventario_id: UUID
    empresa_id: UUID
    ano: int
    abordagem: Abordagem
    gwp_conjunto: str
    estado: str

    @property
    def em_coleta(self) -> bool:
        return self.estado == "em_coleta"

    @property
    def estado_rotulo(self) -> str:
        return ROTULOS_ESTADO.get(self.estado, self.estado)


@dataclass(frozen=True)
class Fonte:
    fonte_id: UUID
    empresa_id: UUID
    inventario_id: UUID
    unidade_id: UUID
    unidade_nome: str
    categoria: str
    descricao: str
    unidade_medida: str
    periodicidade: str
    material: bool
    justificativa: str | None
    ativa: bool
    atividade: str | None = None
    # Unidade dentro do limite organizacional na abordagem do inventario (percentual > 0).
    dentro_do_limite: bool = True

    @property
    def conta_no_progresso(self) -> bool:
        return self.ativa and self.material and self.dentro_do_limite


@dataclass(frozen=True)
class Versao:
    lancamento_id: UUID
    periodo: int
    versao: int
    quantidade: Decimal
    unidade: str
    data_compra: date | None
    observacao: str | None
    origem: str
    criado_por: UUID | None
    criado_em: Any
    decisao: str | None
    motivo: str | None
    decidido_por: UUID | None

    @property
    def situacao(self) -> str:
        """aguardando, aprovado ou devolvido."""
        return self.decisao or "aguardando"


@dataclass
class Progresso:
    esperados: int = 0
    aprovados: int = 0
    aguardando: int = 0
    devolvidos: int = 0
    com_evidencia: int = 0

    def somar(self, outro: "Progresso") -> None:
        self.esperados += outro.esperados
        self.aprovados += outro.aprovados
        self.aguardando += outro.aguardando
        self.devolvidos += outro.devolvidos
        self.com_evidencia += outro.com_evidencia


@dataclass
class SituacaoFonte:
    fonte: Fonte
    atuais: dict[int, Versao] = field(default_factory=dict)
    evidencias_por_periodo: dict[int | None, int] = field(default_factory=dict)
    progresso: Progresso = field(default_factory=Progresso)

    def evidencias_do_periodo(self, periodo: int) -> int:
        return self.evidencias_por_periodo.get(periodo, 0) + self.evidencias_por_periodo.get(
            None, 0
        )


# ---------------------------------------------------------------- validacao


def _texto(valor: str | None) -> str:
    return (valor or "").strip()


def validar_ano(valor: str, hoje: date) -> tuple[int | None, str | None]:
    limpo = _texto(valor)
    if not (limpo.isdigit() and len(limpo) == 4):
        return None, "Informe o ano com quatro dígitos, como 2025."
    ano = int(limpo)
    if not ANO_MINIMO <= ano <= hoje.year:
        return None, f"O ano fica entre {ANO_MINIMO} e {hoje.year}."
    return ano, None


def validar_quantidade(valor: str) -> tuple[Decimal | None, str | None]:
    limpo = _texto(valor)
    if not limpo:
        return None, "Informe a quantidade. Se não houve consumo no período, digite 0."
    try:
        numero = ler_numero(limpo)
    except NumeroInvalido:
        return None, "Use só números, com vírgula para os decimais, como 1.234,5."
    if numero < 0:
        return None, "A quantidade não pode ser negativa."
    expoente = numero.as_tuple().exponent
    if isinstance(expoente, int) and expoente < -CASAS_MAXIMAS:
        return None, f"Use no máximo {CASAS_MAXIMAS} casas decimais."
    if numero.adjusted() >= DIGITOS_INTEIROS_MAXIMOS:
        return None, "Número grande demais. Confira se a unidade está certa."
    return numero, None


def validar_unidade_medida(valor: str) -> tuple[str | None, str | None]:
    limpo = _texto(valor)
    if not limpo:
        return None, "Informe a unidade, como L, m**3, kg, t, kWh ou MWh."
    try:
        validar_unidade_de_atividade(limpo)
    except UnidadeInvalida:
        return None, "Unidade não reconhecida. Use, por exemplo, L, m**3, kg, t, kWh, MWh ou GJ."
    return limpo, None


def validar_data_compra(valor: str, ano: int, hoje: date) -> tuple[date | None, str | None]:
    limpo = _texto(valor)
    if not limpo:
        return None, None
    try:
        if "/" in limpo:
            dia, mes, ano_texto = limpo.split("/")
            data = date(int(ano_texto), int(mes), int(dia))
        else:
            data = date.fromisoformat(limpo)
    except ValueError:
        return None, "Use a data no formato dd/mm/aaaa."
    if data > hoje:
        return None, "A data da compra não pode estar no futuro."
    if data.year < ano - 1 or data.year > ano:
        return None, f"A compra precisa ser de {ano - 1} ou {ano}."
    return data, None


def _texto_opcional(
    coletor: ColetorDeErros, campo: str, valor: str | None, limite: int = TAMANHO_MAXIMO_TEXTO
) -> str | None:
    limpo = _texto(valor)
    if len(limpo) > limite:
        coletor.adicionar(campo, f"Use no máximo {limite} caracteres.")
    return limpo or None


# ---------------------------------------------------------------- inventario


def _inventario(linha: Any) -> Inventario:
    return Inventario(
        linha.inventario_id,
        linha.empresa_id,
        linha.ano,
        Abordagem(linha.abordagem),
        linha.gwp_conjunto,
        linha.estado,
    )


_COLUNAS_INVENTARIO = "inventario_id, empresa_id, ano, abordagem, gwp_conjunto, estado"


async def listar_inventarios(sessao: AsyncSession, empresa_id: UUID) -> list[Inventario]:
    linhas = await sessao.execute(
        text(
            f"SELECT {_COLUNAS_INVENTARIO} FROM inventario"  # noqa: S608  # nosec B608
            " WHERE empresa_id = :empresa ORDER BY ano DESC"
        ),
        {"empresa": empresa_id},
    )
    return [_inventario(linha) for linha in linhas]


async def obter_inventario(sessao: AsyncSession, inventario_id: UUID) -> Inventario:
    linha = (
        await sessao.execute(
            text(
                f"SELECT {_COLUNAS_INVENTARIO} FROM inventario"  # noqa: S608  # nosec B608
                " WHERE inventario_id = :id"
            ),
            {"id": inventario_id},
        )
    ).first()
    if linha is None:
        raise AcessoNegado
    return _inventario(linha)


async def criar_inventario(
    sessao: AsyncSession,
    acesso: Acesso,
    empresa_id: UUID,
    formulario: dict[str, str],
    hoje: date,
) -> UUID:
    ativa = await sessao.scalar(
        text("SELECT ativa FROM empresa WHERE empresa_id = :id"), {"id": empresa_id}
    )
    if ativa is None:
        raise AcessoNegado
    coletor = ColetorDeErros()
    if not ativa:
        coletor.adicionar("empresa_id", "A empresa está desativada. Reative-a na Organização.")
    ano, erro = validar_ano(formulario.get("ano", ""), hoje)
    if erro:
        coletor.adicionar("ano", erro)
    elif await sessao.scalar(
        text("SELECT count(*) FROM inventario WHERE empresa_id = :empresa AND ano = :ano"),
        {"empresa": empresa_id, "ano": ano},
    ):
        coletor.adicionar("ano", "Esta empresa já tem inventário deste ano. Abra o que existe.")
    try:
        abordagem = Abordagem(formulario.get("abordagem", ""))
    except ValueError:
        coletor.adicionar("abordagem", "Escolha a abordagem de consolidação.")
    coletor.levantar_se_houver()

    inventario_id = uuid4()
    await sessao.execute(
        text(
            "INSERT INTO inventario (tenant_id, inventario_id, empresa_id, ano, abordagem,"
            " gwp_conjunto, criado_por) VALUES (:tenant, :id, :empresa, :ano, :abordagem,"
            " :gwp, :usuario)"
        ),
        {
            "tenant": acesso.tenant_id,
            "id": inventario_id,
            "empresa": empresa_id,
            "ano": ano,
            "abordagem": abordagem.value,
            "gwp": GWP_PADRAO,
            "usuario": acesso.usuario_id,
        },
    )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="inventario.criado",
        entidade="inventario",
        entidade_id=inventario_id,
        dados={"empresa_id": str(empresa_id), "ano": ano, "abordagem": abordagem.value},
    )
    return inventario_id


async def inventario_em_foco(sessao: AsyncSession, empresa_id: UUID) -> Inventario | None:
    """O inventario mais recente da empresa: o que aparece na faixa de estado."""
    inventarios = await listar_inventarios(sessao, empresa_id)
    return inventarios[0] if inventarios else None


def _exigir_em_coleta(inventario: Inventario) -> None:
    if not inventario.em_coleta:
        raise ErroValidacao(
            "inventario", "O inventário não está em coleta: os dados só mudam por reapresentação."
        )


# ---------------------------------------------------------------- fontes

_COLUNAS_FONTE = (
    "f.fonte_id, f.empresa_id, f.inventario_id, f.unidade_id, u.nome AS unidade_nome,"
    " f.categoria, f.descricao, f.unidade_medida, f.periodicidade, f.material,"
    " f.justificativa, f.ativa, f.atividade,"
    f" {SQL_DENTRO_DO_LIMITE} AS dentro_do_limite"
)
_DE_FONTE = (
    " FROM fonte_emissao f JOIN unidade u"
    " ON u.tenant_id = f.tenant_id AND u.unidade_id = f.unidade_id"
    " JOIN inventario i ON i.tenant_id = f.tenant_id AND i.inventario_id = f.inventario_id"
)


async def listar_fontes(sessao: AsyncSession, inventario_id: UUID) -> list[Fonte]:
    linhas = await sessao.execute(
        text(
            f"SELECT {_COLUNAS_FONTE}{_DE_FONTE}"  # nosec B608
            " WHERE f.inventario_id = :id ORDER BY f.ativa DESC, u.nome, f.categoria, f.descricao"
        ),
        {"id": inventario_id},
    )
    return [Fonte(**linha._asdict()) for linha in linhas]


async def obter_fonte(sessao: AsyncSession, fonte_id: UUID) -> Fonte:
    linha = (
        await sessao.execute(
            text(
                f"SELECT {_COLUNAS_FONTE}{_DE_FONTE}"  # nosec B608
                " WHERE f.fonte_id = :id"
            ),
            {"id": fonte_id},
        )
    ).first()
    if linha is None:
        raise AcessoNegado
    return Fonte(**linha._asdict())


async def atividades_da_categoria(sessao: AsyncSession, categoria: str) -> dict[str, str]:
    """Codigo -> rotulo das atividades com fator aprovado (ou mistura) para a categoria."""
    linhas = await sessao.execute(
        text(
            "SELECT DISTINCT v.codigo, v.descricao FROM fator_versao v"
            " JOIN lote_decisao d ON d.lote_id = v.lote_id AND d.decisao = 'aprovado'"
            " WHERE v.categoria = :categoria"
            " UNION SELECT DISTINCT m.combustivel, m.combustivel FROM mistura_versao m"
            " JOIN lote_decisao d ON d.lote_id = m.lote_id AND d.decisao = 'aprovado'"
            " WHERE :categoria LIKE 'escopo1.combustao%'"
        ),
        {"categoria": categoria},
    )
    rotulos: dict[str, str] = {}
    for linha in linhas:
        rotulos.setdefault(linha.codigo, f"{linha.descricao} ({linha.codigo})")
    return dict(sorted(rotulos.items(), key=lambda par: par[1].lower()))


async def _validar_fonte(
    sessao: AsyncSession,
    inventario: Inventario,
    formulario: dict[str, str],
    atual: Fonte | None,
) -> dict[str, Any]:
    coletor = ColetorDeErros()
    dados: dict[str, Any] = {}

    if atual is None:
        try:
            unidade_id = UUID(formulario.get("unidade_id", ""))
            ativa = await sessao.scalar(
                text("SELECT ativa FROM unidade WHERE unidade_id = :id AND empresa_id = :empresa"),
                {"id": unidade_id, "empresa": inventario.empresa_id},
            )
        except ValueError:
            ativa = None
        if not ativa:
            coletor.adicionar("unidade_id", "Escolha uma unidade ativa da empresa.")
        else:
            dados["unidade_id"] = unidade_id

        codigo = _texto(formulario.get("categoria"))
        achada = categoria(codigo)
        if achada is None:
            coletor.adicionar("categoria", "Escolha a categoria da fonte.")
        dados["categoria"] = codigo

        unidade_medida, erro = validar_unidade_medida(formulario.get("unidade_medida", ""))
        if erro:
            coletor.adicionar("unidade_medida", erro)
        dados["unidade_medida"] = unidade_medida

        periodicidade = _texto(formulario.get("periodicidade"))
        if periodicidade not in ("mensal", "anual"):
            coletor.adicionar("periodicidade", "Escolha se o dado é mensal ou anual.")
        elif achada is not None and achada.exige_mensal and periodicidade != "mensal":
            coletor.adicionar(
                "periodicidade",
                "Energia elétrica é coletada mês a mês, porque o fator da rede muda a cada mês.",
            )
        dados["periodicidade"] = periodicidade

    descricao = _texto(formulario.get("descricao"))
    if not descricao:
        coletor.adicionar("descricao", "Descreva a fonte, como Caldeira 1 ou Frota de entregas.")
    elif len(descricao) > TAMANHO_MAXIMO_DESCRICAO:
        coletor.adicionar("descricao", f"Use no máximo {TAMANHO_MAXIMO_DESCRICAO} caracteres.")
    dados["descricao"] = descricao

    material = formulario.get("material") == "sim"
    justificativa = _texto_opcional(coletor, "justificativa", formulario.get("justificativa"))
    if not material and len(justificativa or "") < MINIMO_JUSTIFICATIVA:
        coletor.adicionar(
            "justificativa",
            "Explique por que a fonte fica fora do inventário (pelo menos 10 caracteres).",
        )
    dados["material"] = material
    dados["justificativa"] = None if material else justificativa

    # Atividade: o codigo do combustivel ou da atividade nos fatores. Pode ficar para depois
    # (a fonte vira pendencia no calculo), mas so aceita codigo com fator aprovado.
    atividade = _texto(formulario.get("atividade")) or None
    codigo_categoria = atual.categoria if atual is not None else dados.get("categoria", "")
    if atividade is not None and atividade not in await atividades_da_categoria(
        sessao, codigo_categoria
    ):
        coletor.adicionar(
            "atividade", "Escolha um combustível ou atividade da lista, ou deixe para depois."
        )
    dados["atividade"] = atividade
    coletor.levantar_se_houver()
    return dados


async def criar_fonte(
    sessao: AsyncSession, acesso: Acesso, inventario_id: UUID, formulario: dict[str, str]
) -> UUID:
    inventario = await obter_inventario(sessao, inventario_id)
    _exigir_em_coleta(inventario)
    dados = await _validar_fonte(sessao, inventario, formulario, None)
    fonte_id = uuid4()
    await sessao.execute(
        text(
            "INSERT INTO fonte_emissao (tenant_id, fonte_id, empresa_id, inventario_id,"
            " unidade_id, categoria, descricao, unidade_medida, periodicidade, material,"
            " justificativa, atividade) VALUES (:tenant, :id, :empresa, :inventario,"
            " :unidade_id, :categoria, :descricao, :unidade_medida, :periodicidade, :material,"
            " :justificativa, :atividade)"
        ),
        {
            "tenant": acesso.tenant_id,
            "id": fonte_id,
            "empresa": inventario.empresa_id,
            "inventario": inventario_id,
            **dados,
        },
    )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="fonte.criada",
        entidade="fonte_emissao",
        entidade_id=fonte_id,
        dados={
            "empresa_id": str(inventario.empresa_id),
            "inventario_id": str(inventario_id),
            "categoria": dados["categoria"],
            "material": dados["material"],
        },
    )
    return fonte_id


async def alterar_fonte(
    sessao: AsyncSession, acesso: Acesso, fonte_id: UUID, formulario: dict[str, str]
) -> Fonte:
    """Muda descricao, atividade e materialidade. Unidade, categoria e periodicidade ficam: os
    dados ja lancados dependem delas (para trocar, desative e crie outra fonte). Os calculos
    ja feitos guardam a atividade que usaram."""
    fonte = await obter_fonte(sessao, fonte_id)
    inventario = await obter_inventario(sessao, fonte.inventario_id)
    _exigir_em_coleta(inventario)
    dados = await _validar_fonte(sessao, inventario, formulario, fonte)
    alterados = sorted(n for n, v in dados.items() if getattr(fonte, n) != v)
    if alterados:
        await sessao.execute(
            text(
                "UPDATE fonte_emissao SET descricao = :descricao, material = :material,"
                " justificativa = :justificativa, atividade = :atividade, atualizado_em = now()"
                " WHERE fonte_id = :id"
            ),
            {"id": fonte_id, **dados},
        )
        await audit.registrar(
            sessao,
            tenant_id=acesso.tenant_id,
            usuario_id=acesso.usuario_id,
            acao="fonte.alterada",
            entidade="fonte_emissao",
            entidade_id=fonte_id,
            dados={"empresa_id": str(fonte.empresa_id), "campos": alterados},
        )
    return fonte


async def definir_fonte_ativa(
    sessao: AsyncSession, acesso: Acesso, fonte_id: UUID, *, ativa: bool
) -> Fonte:
    fonte = await obter_fonte(sessao, fonte_id)
    _exigir_em_coleta(await obter_inventario(sessao, fonte.inventario_id))
    await sessao.execute(
        text("UPDATE fonte_emissao SET ativa = :ativa, atualizado_em = now() WHERE fonte_id = :id"),
        {"id": fonte_id, "ativa": ativa},
    )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="fonte.reativada" if ativa else "fonte.desativada",
        entidade="fonte_emissao",
        entidade_id=fonte_id,
        dados={"empresa_id": str(fonte.empresa_id)},
    )
    return fonte


# ---------------------------------------------------------------- lancamentos

# Colunas fixas da versao, na ordem dos campos de Versao, com fonte_id em segundo (agrupa).
_COLUNAS_VERSAO = (
    "l.lancamento_id", "l.fonte_id", "l.periodo", "l.versao", "l.quantidade", "l.unidade",
    "l.data_compra", "l.observacao", "l.origem", "l.criado_por", "l.criado_em",
    "d.decisao", "d.motivo", "d.decidido_por",
)  # fmt: skip
_SQL_VERSOES = (
    f"SELECT {', '.join(_COLUNAS_VERSAO)}"  # noqa: S608  # nosec B608
    " FROM lancamento l LEFT JOIN lancamento_decisao d"
    "   ON d.tenant_id = l.tenant_id AND d.lancamento_id = l.lancamento_id"
)
if [c.split(".")[1] for c in _COLUNAS_VERSAO if c != "l.fonte_id"] != [
    campo.name for campo in fields(Versao)
]:
    raise RuntimeError("_COLUNAS_VERSAO fora da ordem dos campos de Versao")


def _versao(linha: Any) -> Versao:
    # Por posicao: row._asdict() e o acesso por nome custavam mais que a consulta no painel.
    lancamento_id, _fonte_id, *resto = linha
    return Versao(lancamento_id, *resto)


async def historico(sessao: AsyncSession, fonte_id: UUID, periodo: int) -> list[Versao]:
    linhas = await sessao.execute(
        text(
            _SQL_VERSOES + " WHERE l.fonte_id = :fonte AND l.periodo = :periodo"
            " ORDER BY l.versao DESC"
        ),
        {"fonte": fonte_id, "periodo": periodo},
    )
    return [_versao(linha) for linha in linhas]


async def versoes_atuais(
    sessao: AsyncSession, fonte_ids: list[UUID]
) -> dict[tuple[UUID, int], Versao]:
    """Versao mais recente de cada (fonte, periodo), numa consulta so."""
    if not fonte_ids:
        return {}
    linhas = await sessao.execute(
        text(
            "SELECT DISTINCT ON (l.fonte_id, l.periodo)"
            + _SQL_VERSOES.removeprefix("SELECT")
            + " WHERE l.fonte_id = ANY(:ids) ORDER BY l.fonte_id, l.periodo, l.versao DESC"
        ),
        {"ids": fonte_ids},
    )
    return {(linha.fonte_id, linha.periodo): _versao(linha) for linha in linhas}


async def obter_lancamento(sessao: AsyncSession, lancamento_id: UUID) -> tuple[UUID, Versao]:
    linha = (
        await sessao.execute(
            text(_SQL_VERSOES + " WHERE l.lancamento_id = :id"), {"id": lancamento_id}
        )
    ).first()
    if linha is None:
        raise AcessoNegado
    return linha.fonte_id, _versao(linha)


@dataclass(frozen=True)
class NovoLancamento:
    periodo: int
    quantidade: Decimal
    unidade: str
    data_compra: date | None
    observacao: str | None


def validar_lancamento(
    fonte: Fonte, inventario: Inventario, formulario: dict[str, str], hoje: date
) -> NovoLancamento:
    coletor = ColetorDeErros()
    periodo_texto = _texto(formulario.get("periodo"))
    periodo = int(periodo_texto) if periodo_texto.isdigit() else -1
    if periodo not in periodos(fonte.periodicidade):
        coletor.adicionar("periodo", "Escolha o período do dado.")
    quantidade, erro = validar_quantidade(formulario.get("quantidade", ""))
    if erro:
        coletor.adicionar("quantidade", erro)
    unidade = _texto(formulario.get("unidade")) or fonte.unidade_medida
    unidade_valida, erro = validar_unidade_medida(unidade)
    if erro:
        coletor.adicionar("unidade", erro)
    elif unidade_valida and not mesma_dimensao(unidade_valida, fonte.unidade_medida):
        coletor.adicionar(
            "unidade",
            f"A fonte é medida em {fonte.unidade_medida}. Use uma unidade da mesma grandeza.",
        )
    data_compra, erro = validar_data_compra(formulario.get("data_compra", ""), inventario.ano, hoje)
    if erro:
        coletor.adicionar("data_compra", erro)
    observacao = _texto_opcional(coletor, "observacao", formulario.get("observacao"))
    coletor.levantar_se_houver()
    if quantidade is None:  # o coletor ja recusou o caso vazio; aqui so para o tipo
        raise ErroValidacao("quantidade", "Informe a quantidade.")
    return NovoLancamento(periodo, quantidade, unidade, data_compra, observacao)


def igual_ao_atual(atual: Versao | None, novo: NovoLancamento) -> bool:
    return (
        atual is not None
        and atual.decisao != "devolvido"
        and atual.quantidade == novo.quantidade
        and atual.unidade == novo.unidade
        and atual.data_compra == novo.data_compra
        and (atual.observacao or None) == novo.observacao
    )


async def gravar_lancamento(
    sessao: AsyncSession,
    acesso: Acesso,
    fonte: Fonte,
    novo: NovoLancamento,
    *,
    origem: str = "manual",
    planilha_id: UUID | None = None,
) -> UUID | None:
    """Grava uma versao nova. Devolve None se o dado e igual ao atual (nada a gravar)."""
    if not fonte.ativa:
        raise ErroValidacao("fonte", "A fonte está desativada. Reative-a para lançar dados.")
    anteriores = await historico(sessao, fonte.fonte_id, novo.periodo)
    atual = anteriores[0] if anteriores else None
    if igual_ao_atual(atual, novo):
        return None
    return await gravar_versao(
        sessao, acesso, fonte, novo, atual, origem=origem, planilha_id=planilha_id
    )


async def gravar_versao(
    sessao: AsyncSession,
    acesso: Acesso,
    fonte: Fonte,
    novo: NovoLancamento,
    atual: Versao | None,
    *,
    origem: str,
    planilha_id: UUID | None = None,
) -> UUID:
    """Grava a versao seguinte a `atual` (ja lida por quem chama). Se outra pessoa gravou o
    mesmo periodo no meio tempo, a chave unica recusa e a pessoa e avisada."""
    lancamento_id = uuid4()
    versao = (atual.versao + 1) if atual else 1
    try:
        async with sessao.begin_nested():
            await sessao.execute(
                text(
                    "INSERT INTO lancamento (tenant_id, lancamento_id, empresa_id, fonte_id,"
                    " periodo, versao, quantidade, unidade, data_compra, observacao, origem,"
                    " planilha_id, criado_por) VALUES (:tenant, :id, :empresa, :fonte,"
                    " :periodo, :versao, :quantidade, :unidade, :data_compra, :observacao,"
                    " :origem, :planilha, :usuario)"
                ),
                {
                    "tenant": acesso.tenant_id,
                    "id": lancamento_id,
                    "empresa": fonte.empresa_id,
                    "fonte": fonte.fonte_id,
                    "periodo": novo.periodo,
                    "versao": versao,
                    "quantidade": novo.quantidade,
                    "unidade": novo.unidade,
                    "data_compra": novo.data_compra,
                    "observacao": novo.observacao,
                    "origem": origem,
                    "planilha": planilha_id,
                    "usuario": acesso.usuario_id,
                },
            )
    except IntegrityError as erro:
        raise ErroValidacao(
            "quantidade",
            "Outra pessoa gravou este período agora mesmo. Atualize a página e confira.",
        ) from erro
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="lancamento.enviado",
        entidade="lancamento",
        entidade_id=lancamento_id,
        dados={
            "empresa_id": str(fonte.empresa_id),
            "fonte_id": str(fonte.fonte_id),
            "periodo": novo.periodo,
            "versao": versao,
            "origem": origem,
        },
    )
    return lancamento_id


async def lancar(
    sessao: AsyncSession, acesso: Acesso, fonte_id: UUID, formulario: dict[str, str], hoje: date
) -> tuple[Fonte, int, bool]:
    """(fonte, periodo, gravou)."""
    fonte = await obter_fonte(sessao, fonte_id)
    inventario = await obter_inventario(sessao, fonte.inventario_id)
    _exigir_em_coleta(inventario)
    novo = validar_lancamento(fonte, inventario, formulario, hoje)
    gravado = await gravar_lancamento(sessao, acesso, fonte, novo)
    return fonte, novo.periodo, gravado is not None


async def decidir(
    sessao: AsyncSession,
    acesso: Acesso,
    lancamento_id: UUID,
    *,
    aprovar: bool,
    motivo: str = "",
) -> tuple[UUID, int]:
    """Aprova ou devolve a versao mais recente. Devolve (fonte_id, periodo)."""
    fonte_id, versao = await obter_lancamento(sessao, lancamento_id)
    fonte = await obter_fonte(sessao, fonte_id)
    _exigir_em_coleta(await obter_inventario(sessao, fonte.inventario_id))
    if versao.decisao is not None:
        raise ErroValidacao("lancamento", "Este dado já foi decidido. Atualize a página.")
    mais_recente = (await historico(sessao, fonte_id, versao.periodo))[0]
    if mais_recente.lancamento_id != lancamento_id:
        raise ErroValidacao("lancamento", "Existe uma versão mais nova deste dado. Atualize.")
    if versao.criado_por == acesso.usuario_id:
        raise ErroValidacao(
            "lancamento", "Quem lançou o dado não pode aprová-lo. Peça a outra pessoa."
        )
    texto_motivo = _texto(motivo)
    if not aprovar and len(texto_motivo) < MINIMO_MOTIVO:
        raise ErroValidacao("motivo", "Diga o que precisa ser corrigido (pelo menos 5 caracteres).")
    if len(texto_motivo) > TAMANHO_MAXIMO_TEXTO:
        raise ErroValidacao("motivo", f"Use no máximo {TAMANHO_MAXIMO_TEXTO} caracteres.")
    decisao = "aprovado" if aprovar else "devolvido"
    await sessao.execute(
        text(
            "INSERT INTO lancamento_decisao (tenant_id, lancamento_id, empresa_id, decisao,"
            " motivo, decidido_por) VALUES (:tenant, :id, :empresa, :decisao, :motivo, :usuario)"
        ),
        {
            "tenant": acesso.tenant_id,
            "id": lancamento_id,
            "empresa": fonte.empresa_id,
            "decisao": decisao,
            "motivo": None if aprovar else texto_motivo,
            "usuario": acesso.usuario_id,
        },
    )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="lancamento.aprovado" if aprovar else "lancamento.devolvido",
        entidade="lancamento",
        entidade_id=lancamento_id,
        dados={"empresa_id": str(fonte.empresa_id), "fonte_id": str(fonte_id)},
    )
    return fonte_id, versao.periodo


# ---------------------------------------------------------------- situacao e progresso


async def situacao_das_fontes(
    sessao: AsyncSession, inventario_id: UUID, fonte_id: UUID | None = None
) -> list[SituacaoFonte]:
    """Versao atual de cada periodo, evidencias e progresso de cada fonte."""
    fontes = await listar_fontes(sessao, inventario_id)
    if fonte_id is not None:
        fontes = [f for f in fontes if f.fonte_id == fonte_id]
    return await _situacoes(sessao, fontes)


async def progresso_dos_inventarios(
    sessao: AsyncSession, inventario_ids: list[UUID]
) -> dict[UUID, Progresso]:
    """Progresso total de varios inventarios com as mesmas tres consultas (painel)."""
    if not inventario_ids:
        return {}
    linhas = await sessao.execute(
        text(
            f"SELECT {_COLUNAS_FONTE}{_DE_FONTE}"  # nosec B608
            " WHERE f.inventario_id = ANY(:ids)"
            " ORDER BY f.ativa DESC, u.nome, f.categoria, f.descricao"
        ),
        {"ids": inventario_ids},
    )
    por_inventario: dict[UUID, list[SituacaoFonte]] = defaultdict(list)
    for situacao in await _situacoes(sessao, [Fonte(**linha._asdict()) for linha in linhas]):
        por_inventario[situacao.fonte.inventario_id].append(situacao)
    return {i: progresso_total(por_inventario.get(i, [])) for i in inventario_ids}


async def _situacoes(sessao: AsyncSession, fontes: list[Fonte]) -> list[SituacaoFonte]:
    situacoes = {f.fonte_id: SituacaoFonte(f) for f in fontes}
    if not situacoes:
        return []
    ids = list(situacoes)
    for (fonte, periodo), versao in (await versoes_atuais(sessao, ids)).items():
        situacoes[fonte].atuais[periodo] = versao
    contagem: dict[UUID, dict[int | None, int]] = defaultdict(dict)
    for linha in await sessao.execute(
        text(
            "SELECT fonte_id, periodo, count(*) AS total FROM evidencia"
            " WHERE fonte_id = ANY(:ids) GROUP BY fonte_id, periodo"
        ),
        {"ids": ids},
    ):
        contagem[linha.fonte_id][linha.periodo] = linha.total
    for situacao in situacoes.values():
        situacao.evidencias_por_periodo = contagem.get(situacao.fonte.fonte_id, {})
        if not situacao.fonte.conta_no_progresso:
            continue
        progresso = situacao.progresso
        for periodo in periodos(situacao.fonte.periodicidade):
            progresso.esperados += 1
            atual = situacao.atuais.get(periodo)
            if atual is not None:
                if atual.decisao == "aprovado":
                    progresso.aprovados += 1
                elif atual.decisao == "devolvido":
                    progresso.devolvidos += 1
                else:
                    progresso.aguardando += 1
            if situacao.evidencias_do_periodo(periodo):
                progresso.com_evidencia += 1
    return list(situacoes.values())


def progresso_total(situacoes: list[SituacaoFonte]) -> Progresso:
    total = Progresso()
    for situacao in situacoes:
        total.somar(situacao.progresso)
    return total
