"""Grupos, empresas e unidades. Toda consulta passa pelo RLS da sessao de tenant.

Quem pode o que (conferido nas rotas):
- administrador e gestor: grupos, empresas e unidades;
- responsavel: unidades das empresas atribuidas (o RLS barra as demais);
- colaborador: so leitura.
Nada some: empresa e unidade se desativam. Toda mudanca vai para a trilha, so com os nomes
dos campos alterados (sem valores).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.erros import AcessoNegado, ColetorDeErros
from app.core.security.acesso import Acesso
from app.modules.organizacao.validacao import (
    UFS,
    cnae_no_formato,
    cnpj_valido,
    normalizar_cnpj,
)
from app.web.formatos import NumeroInvalido, ler_numero

TAMANHO_MAXIMO_NOME = 200


@dataclass(frozen=True)
class Grupo:
    grupo_id: UUID
    nome: str


@dataclass(frozen=True)
class Empresa:
    empresa_id: UUID
    grupo_id: UUID
    nome: str
    nome_fantasia: str | None
    cnpj: str | None
    cnae: str | None
    uf: str | None
    municipio: str | None
    ativa: bool


@dataclass(frozen=True)
class Unidade:
    unidade_id: UUID
    empresa_id: UUID
    nome: str
    cnpj: str | None
    uf: str
    municipio: str
    participacao_societaria: Decimal
    controle_operacional: bool
    controle_financeiro: bool
    ativa: bool


@dataclass(frozen=True)
class EmpresaResumo:
    empresa_id: UUID
    nome: str
    cnpj: str | None
    ativa: bool
    unidades_ativas: int


@dataclass(frozen=True)
class GrupoArvore:
    grupo_id: UUID
    nome: str
    empresas: list[EmpresaResumo]


def _campos_alterados(antes: Any, depois: dict[str, Any]) -> list[str]:
    return sorted(nome for nome in depois if getattr(antes, nome) != depois[nome])


def _texto(valor: str | None) -> str:
    return (valor or "").strip()


def _nome(coletor: ColetorDeErros, campo: str, valor: str, rotulo: str) -> str:
    limpo = _texto(valor)
    if not limpo:
        coletor.adicionar(campo, f"Informe {rotulo}.")
    elif len(limpo) > TAMANHO_MAXIMO_NOME:
        coletor.adicionar(campo, f"Use no máximo {TAMANHO_MAXIMO_NOME} caracteres.")
    return limpo


def validar_cnpj(valor: str, *, obrigatorio: bool) -> tuple[str | None, str | None]:
    """(cnpj normalizado, mensagem de erro)."""
    limpo = normalizar_cnpj(valor or "")
    if not limpo:
        return None, ("Informe o CNPJ." if obrigatorio else None)
    if not cnpj_valido(limpo):
        return None, "CNPJ inválido. Confira os 14 caracteres e os dois dígitos finais."
    return limpo, None


def validar_cnae(valor: str) -> tuple[str | None, str | None]:
    limpo = _texto(valor)
    if not limpo:
        return None, None
    if not cnae_no_formato(limpo):
        return None, "Use o formato da subclasse CNAE, como 2512-8/00."
    return limpo, None


def validar_participacao(valor: str) -> tuple[Decimal | None, str | None]:
    limpo = _texto(valor)
    if not limpo:
        return None, "Informe a participação societária. Se for 100%, digite 100."
    try:
        numero = ler_numero(limpo)
    except NumeroInvalido:
        return None, "Use só números, com vírgula para os decimais, como 51,5."
    if not Decimal(0) <= numero <= Decimal(100):
        return None, "A participação fica entre 0 e 100."
    if numero.as_tuple().exponent < -4:  # type: ignore[operator]
        return None, "Use no máximo 4 casas decimais."
    return numero, None


def _uf(coletor: ColetorDeErros, valor: str) -> str:
    limpo = _texto(valor).upper()
    if limpo not in UFS:
        coletor.adicionar("uf", "Escolha a UF.")
    return limpo


# ---------------------------------------------------------------- leitura


async def listar_arvore(sessao: AsyncSession) -> list[GrupoArvore]:
    grupos = {
        linha.grupo_id: GrupoArvore(linha.grupo_id, linha.nome, [])
        for linha in await sessao.execute(text("SELECT grupo_id, nome FROM grupo ORDER BY nome"))
    }
    linhas = await sessao.execute(
        text(
            "SELECT e.empresa_id, e.grupo_id, e.nome, e.cnpj, e.ativa,"
            " count(u.unidade_id) FILTER (WHERE u.ativa) AS unidades_ativas"
            " FROM empresa e LEFT JOIN unidade u"
            "   ON u.tenant_id = e.tenant_id AND u.empresa_id = e.empresa_id"
            " GROUP BY e.empresa_id, e.grupo_id, e.nome, e.cnpj, e.ativa"
            " ORDER BY e.nome"
        )
    )
    for linha in linhas:
        grupo = grupos.get(linha.grupo_id)
        if grupo is not None:
            grupo.empresas.append(
                EmpresaResumo(
                    linha.empresa_id, linha.nome, linha.cnpj, linha.ativa, linha.unidades_ativas
                )
            )
    return list(grupos.values())


async def listar_grupos(sessao: AsyncSession) -> list[Grupo]:
    linhas = await sessao.execute(text("SELECT grupo_id, nome FROM grupo ORDER BY nome"))
    return [Grupo(linha.grupo_id, linha.nome) for linha in linhas]


async def obter_grupo(sessao: AsyncSession, grupo_id: UUID) -> Grupo:
    linha = (
        await sessao.execute(
            text("SELECT grupo_id, nome FROM grupo WHERE grupo_id = :id"), {"id": grupo_id}
        )
    ).first()
    if linha is None:
        raise AcessoNegado
    return Grupo(linha.grupo_id, linha.nome)


async def obter_empresa(sessao: AsyncSession, empresa_id: UUID) -> Empresa:
    linha = (
        await sessao.execute(
            text(
                "SELECT empresa_id, grupo_id, nome, nome_fantasia, cnpj, cnae, uf, municipio, ativa"
                " FROM empresa WHERE empresa_id = :id"
            ),
            {"id": empresa_id},
        )
    ).first()
    if linha is None:
        raise AcessoNegado
    return Empresa(**linha._asdict())


async def listar_unidades(sessao: AsyncSession, empresa_id: UUID) -> list[Unidade]:
    linhas = await sessao.execute(
        text(
            "SELECT unidade_id, empresa_id, nome, cnpj, uf, municipio, participacao_societaria,"
            " controle_operacional, controle_financeiro, ativa"
            " FROM unidade WHERE empresa_id = :empresa ORDER BY ativa DESC, nome"
        ),
        {"empresa": empresa_id},
    )
    return [Unidade(**linha._asdict()) for linha in linhas]


async def obter_unidade(sessao: AsyncSession, unidade_id: UUID) -> Unidade:
    linha = (
        await sessao.execute(
            text(
                "SELECT unidade_id, empresa_id, nome, cnpj, uf, municipio, participacao_societaria,"
                " controle_operacional, controle_financeiro, ativa"
                " FROM unidade WHERE unidade_id = :id"
            ),
            {"id": unidade_id},
        )
    ).first()
    if linha is None:
        raise AcessoNegado
    return Unidade(**linha._asdict())


# ---------------------------------------------------------------- grupo


async def salvar_grupo(
    sessao: AsyncSession, acesso: Acesso, nome: str, grupo_id: UUID | None = None
) -> UUID:
    coletor = ColetorDeErros()
    limpo = _nome(coletor, "nome", nome, "o nome do grupo")
    if not coletor.tem("nome"):
        repetido = await sessao.scalar(
            text("SELECT count(*) FROM grupo WHERE lower(nome) = lower(:nome) AND grupo_id <> :id"),
            {"nome": limpo, "id": grupo_id or uuid4()},
        )
        if repetido:
            coletor.adicionar("nome", "Já existe um grupo com esse nome neste cliente.")
    coletor.levantar_se_houver()

    if grupo_id is None:
        grupo_id = uuid4()
        await sessao.execute(
            text("INSERT INTO grupo (tenant_id, grupo_id, nome) VALUES (:tenant, :id, :nome)"),
            {"tenant": acesso.tenant_id, "id": grupo_id, "nome": limpo},
        )
        acao, dados = "grupo.criado", {}
    else:
        await obter_grupo(sessao, grupo_id)
        await sessao.execute(
            text("UPDATE grupo SET nome = :nome, atualizado_em = now() WHERE grupo_id = :id"),
            {"id": grupo_id, "nome": limpo},
        )
        acao, dados = "grupo.alterado", {"campos": ["nome"]}
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao=acao,
        entidade="grupo",
        entidade_id=grupo_id,
        dados=dados,
    )
    return grupo_id


# ---------------------------------------------------------------- empresa


async def _validar_empresa(
    sessao: AsyncSession, formulario: dict[str, str], empresa_id: UUID | None
) -> dict[str, Any]:
    coletor = ColetorDeErros()
    dados: dict[str, Any] = {
        "nome": _nome(coletor, "nome", formulario.get("nome", ""), "a razão social"),
        "nome_fantasia": _texto(formulario.get("nome_fantasia")) or None,
        "uf": _uf(coletor, formulario.get("uf", "")),
        "municipio": _nome(
            coletor, "municipio", formulario.get("municipio", ""), "o município da sede"
        ),
    }
    try:
        dados["grupo_id"] = UUID(formulario.get("grupo_id", ""))
        await obter_grupo(sessao, dados["grupo_id"])
    except (ValueError, AcessoNegado):
        coletor.adicionar("grupo_id", "Escolha o grupo da empresa.")

    cnpj, erro = validar_cnpj(formulario.get("cnpj", ""), obrigatorio=True)
    if erro:
        coletor.adicionar("cnpj", erro)
    elif await sessao.scalar(
        text("SELECT count(*) FROM empresa WHERE cnpj = :cnpj AND empresa_id <> :id"),
        {"cnpj": cnpj, "id": empresa_id or uuid4()},
    ):
        coletor.adicionar("cnpj", "Já existe uma empresa com este CNPJ neste cliente.")
    dados["cnpj"] = cnpj

    cnae, erro = validar_cnae(formulario.get("cnae", ""))
    if erro:
        coletor.adicionar("cnae", erro)
    dados["cnae"] = cnae
    coletor.levantar_se_houver()
    return dados


async def criar_empresa(sessao: AsyncSession, acesso: Acesso, formulario: dict[str, str]) -> UUID:
    dados = await _validar_empresa(sessao, formulario, None)
    empresa_id = uuid4()
    await sessao.execute(
        text(
            "INSERT INTO empresa (tenant_id, empresa_id, grupo_id, nome, nome_fantasia, cnpj,"
            " cnae, uf, municipio) VALUES (:tenant, :id, :grupo_id, :nome, :nome_fantasia,"
            " :cnpj, :cnae, :uf, :municipio)"
        ),
        {"tenant": acesso.tenant_id, "id": empresa_id, **dados},
    )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="empresa.criada",
        entidade="empresa",
        entidade_id=empresa_id,
        dados={"grupo_id": str(dados["grupo_id"])},
    )
    return empresa_id


async def alterar_empresa(
    sessao: AsyncSession, acesso: Acesso, empresa_id: UUID, formulario: dict[str, str]
) -> None:
    antes = await obter_empresa(sessao, empresa_id)
    dados = await _validar_empresa(sessao, formulario, empresa_id)
    alterados = _campos_alterados(antes, dados)
    if not alterados:
        return
    await sessao.execute(
        text(
            "UPDATE empresa SET grupo_id = :grupo_id, nome = :nome, nome_fantasia = :nome_fantasia,"
            " cnpj = :cnpj, cnae = :cnae, uf = :uf, municipio = :municipio, atualizado_em = now()"
            " WHERE empresa_id = :id"
        ),
        {"id": empresa_id, **dados},
    )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="empresa.alterada",
        entidade="empresa",
        entidade_id=empresa_id,
        dados={"campos": alterados},
    )


async def definir_empresa_ativa(
    sessao: AsyncSession, acesso: Acesso, empresa_id: UUID, *, ativa: bool
) -> None:
    await obter_empresa(sessao, empresa_id)
    await sessao.execute(
        text("UPDATE empresa SET ativa = :ativa, atualizado_em = now() WHERE empresa_id = :id"),
        {"id": empresa_id, "ativa": ativa},
    )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="empresa.reativada" if ativa else "empresa.desativada",
        entidade="empresa",
        entidade_id=empresa_id,
    )


# ---------------------------------------------------------------- unidade


async def _validar_unidade(
    sessao: AsyncSession, formulario: dict[str, str], empresa_id: UUID, unidade_id: UUID | None
) -> dict[str, Any]:
    coletor = ColetorDeErros()
    dados: dict[str, Any] = {
        "nome": _nome(coletor, "nome", formulario.get("nome", ""), "o nome da unidade"),
        "uf": _uf(coletor, formulario.get("uf", "")),
        "municipio": _nome(coletor, "municipio", formulario.get("municipio", ""), "o município"),
        "controle_operacional": formulario.get("controle_operacional") == "sim",
        "controle_financeiro": formulario.get("controle_financeiro") == "sim",
    }
    if not coletor.tem("nome") and await sessao.scalar(
        text(
            "SELECT count(*) FROM unidade WHERE empresa_id = :empresa"
            " AND lower(nome) = lower(:nome) AND unidade_id <> :id"
        ),
        {"empresa": empresa_id, "nome": dados["nome"], "id": unidade_id or uuid4()},
    ):
        coletor.adicionar("nome", "Já existe uma unidade com esse nome nesta empresa.")

    cnpj, erro = validar_cnpj(formulario.get("cnpj", ""), obrigatorio=False)
    if erro:
        coletor.adicionar("cnpj", erro)
    elif cnpj and await sessao.scalar(
        text("SELECT count(*) FROM unidade WHERE cnpj = :cnpj AND unidade_id <> :id"),
        {"cnpj": cnpj, "id": unidade_id or uuid4()},
    ):
        coletor.adicionar("cnpj", "Já existe uma unidade com este CNPJ neste cliente.")
    dados["cnpj"] = cnpj

    participacao, erro = validar_participacao(formulario.get("participacao_societaria", ""))
    if erro:
        coletor.adicionar("participacao_societaria", erro)
    dados["participacao_societaria"] = participacao
    coletor.levantar_se_houver()
    return dados


async def criar_unidade(
    sessao: AsyncSession, acesso: Acesso, empresa_id: UUID, formulario: dict[str, str]
) -> UUID:
    empresa = await obter_empresa(sessao, empresa_id)
    dados = await _validar_unidade(sessao, formulario, empresa.empresa_id, None)
    unidade_id = uuid4()
    await sessao.execute(
        text(
            "INSERT INTO unidade (tenant_id, unidade_id, empresa_id, nome, cnpj, uf, municipio,"
            " participacao_societaria, controle_operacional, controle_financeiro)"
            " VALUES (:tenant, :id, :empresa, :nome, :cnpj, :uf, :municipio,"
            " :participacao_societaria, :controle_operacional, :controle_financeiro)"
        ),
        {"tenant": acesso.tenant_id, "id": unidade_id, "empresa": empresa.empresa_id, **dados},
    )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="unidade.criada",
        entidade="unidade",
        entidade_id=unidade_id,
        dados={"empresa_id": str(empresa.empresa_id)},
    )
    return unidade_id


async def alterar_unidade(
    sessao: AsyncSession, acesso: Acesso, unidade_id: UUID, formulario: dict[str, str]
) -> Unidade:
    antes = await obter_unidade(sessao, unidade_id)
    dados = await _validar_unidade(sessao, formulario, antes.empresa_id, unidade_id)
    alterados = _campos_alterados(antes, dados)
    if alterados:
        await sessao.execute(
            text(
                "UPDATE unidade SET nome = :nome, cnpj = :cnpj, uf = :uf, municipio = :municipio,"
                " participacao_societaria = :participacao_societaria,"
                " controle_operacional = :controle_operacional,"
                " controle_financeiro = :controle_financeiro, atualizado_em = now()"
                " WHERE unidade_id = :id"
            ),
            {"id": unidade_id, **dados},
        )
        await audit.registrar(
            sessao,
            tenant_id=acesso.tenant_id,
            usuario_id=acesso.usuario_id,
            acao="unidade.alterada",
            entidade="unidade",
            entidade_id=unidade_id,
            dados={"campos": alterados, "empresa_id": str(antes.empresa_id)},
        )
    return antes


async def definir_unidade_ativa(
    sessao: AsyncSession, acesso: Acesso, unidade_id: UUID, *, ativa: bool
) -> Unidade:
    unidade = await obter_unidade(sessao, unidade_id)
    await sessao.execute(
        text("UPDATE unidade SET ativa = :ativa, atualizado_em = now() WHERE unidade_id = :id"),
        {"id": unidade_id, "ativa": ativa},
    )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="unidade.reativada" if ativa else "unidade.desativada",
        entidade="unidade",
        entidade_id=unidade_id,
        dados={"empresa_id": str(unidade.empresa_id)},
    )
    return unidade
