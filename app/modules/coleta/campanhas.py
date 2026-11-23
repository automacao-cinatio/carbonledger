"""Campanhas de coleta: abrir com prazo, avisar a equipe e lembrar ate a coleta acabar.

asyncpg (a mesma funcao serve a tela e ao worker). O envio de e-mail e injetado (enviar), para
o worker e os testes. O e-mail leva so a lista do que falta (fonte e periodo), sem numeros.

Destinatarios: responsaveis e colaboradores ativos da empresa; sem ninguem atribuido, a gestao
(administradores e gestores ativos). Lembretes: a 7, 3 e 1 dia do prazo e no vencimento, cada
um uma vez so (campanha_envio). A campanha se encerra sozinha quando a coleta fica completa ou o
inventario e fechado.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from app.core import audit
from app.core.erros import AcessoNegado, ColetorDeErros, ErroValidacao
from app.modules.coleta.categorias import periodos, rotulo_periodo

Enviar = Callable[[str, str, str], Awaitable[None]]

TAMANHO_MAXIMO_MENSAGEM = 1000
MAXIMO_PENDENCIAS_NO_EMAIL = 50
LEMBRETES = ((7, "lembrete_7"), (3, "lembrete_3"), (1, "lembrete_1"))


@dataclass(frozen=True)
class Campanha:
    campanha_id: UUID
    empresa_id: UUID
    inventario_id: UUID
    prazo: date
    mensagem: str | None
    encerrada_em: Any
    motivo_encerramento: str | None


async def aberta(conexao: Any, inventario_id: UUID) -> Campanha | None:
    linha = await conexao.fetchrow(
        "SELECT campanha_id, empresa_id, inventario_id, prazo, mensagem, encerrada_em,"
        " motivo_encerramento FROM campanha WHERE inventario_id = $1 AND encerrada_em IS NULL",
        inventario_id,
    )
    return None if linha is None else Campanha(**dict(linha))


async def envios(conexao: Any, campanha_id: UUID) -> list[Any]:
    return list(
        await conexao.fetch(
            "SELECT tipo, destinatarios, enviado_em FROM campanha_envio WHERE campanha_id = $1"
            " ORDER BY enviado_em",
            campanha_id,
        )
    )


async def destinatarios(conexao: Any, empresa_id: UUID) -> list[str]:
    equipe = await conexao.fetch(
        "SELECT DISTINCT u.email FROM membro m JOIN usuario u ON u.usuario_id = m.usuario_id"
        " JOIN membro_empresa me ON me.tenant_id = m.tenant_id AND me.usuario_id = m.usuario_id"
        " WHERE m.ativo AND m.perfil IN ('responsavel', 'colaborador') AND me.empresa_id = $1"
        " ORDER BY u.email",
        empresa_id,
    )
    if equipe:
        return [linha["email"] for linha in equipe]
    gestao = await conexao.fetch(
        "SELECT DISTINCT u.email FROM membro m JOIN usuario u ON u.usuario_id = m.usuario_id"
        " WHERE m.ativo AND m.perfil IN ('administrador', 'gestor') ORDER BY u.email"
    )
    return [linha["email"] for linha in gestao]


async def pendencias(conexao: Any, inventario_id: UUID) -> list[str]:
    """Periodos de fontes ativas e materiais sem dado aprovado, em texto para o e-mail."""
    fontes = await conexao.fetch(
        "SELECT fonte_id, descricao, periodicidade FROM fonte_emissao"
        " WHERE inventario_id = $1 AND ativa AND material ORDER BY descricao, fonte_id",
        inventario_id,
    )
    atuais = {
        (linha["fonte_id"], linha["periodo"]): linha["decisao"]
        for linha in await conexao.fetch(
            "SELECT DISTINCT ON (l.fonte_id, l.periodo) l.fonte_id, l.periodo, d.decisao"
            " FROM lancamento l LEFT JOIN lancamento_decisao d"
            "   ON d.tenant_id = l.tenant_id AND d.lancamento_id = l.lancamento_id"
            " WHERE l.fonte_id = ANY($1::uuid[]) ORDER BY l.fonte_id, l.periodo, l.versao DESC",
            [f["fonte_id"] for f in fontes],
        )
    }
    faltando: list[str] = []
    for fonte in fontes:
        for periodo in periodos(fonte["periodicidade"]):
            chave = (fonte["fonte_id"], periodo)
            if chave not in atuais:
                situacao = "sem dado"
            elif atuais[chave] == "devolvido":
                situacao = "devolvido para correção"
            elif atuais[chave] is None:
                situacao = "aguardando aprovação"
            else:
                continue
            faltando.append(f"{fonte['descricao']}: {rotulo_periodo(periodo)} ({situacao})")
    return faltando


def _texto(
    tipo: str, empresa: str, ano: int, prazo: date, mensagem: str | None, faltando: list[str]
) -> tuple[str, str]:
    data = prazo.strftime("%d/%m/%Y")
    titulos = {
        "abertura": f"Coleta do inventário {ano} de {empresa}: prazo {data}",
        "lembrete_7": f"Faltam 7 dias para a coleta de {empresa} ({data})",
        "lembrete_3": f"Faltam 3 dias para a coleta de {empresa} ({data})",
        "lembrete_1": f"Falta 1 dia para a coleta de {empresa} ({data})",
        "vencida": f"O prazo da coleta de {empresa} venceu ({data})",
    }
    lista = faltando[:MAXIMO_PENDENCIAS_NO_EMAIL]
    if len(faltando) > MAXIMO_PENDENCIAS_NO_EMAIL:
        lista.append(f"E mais {len(faltando) - MAXIMO_PENDENCIAS_NO_EMAIL} itens.")
    corpo = (
        f"A coleta de dados do inventário de emissões {ano} de {empresa} vai até {data}.\n\n"
        + (f"Recado da gestão: {mensagem}\n\n" if mensagem else "")
        + "O que ainda falta:\n"
        + "\n".join(f"- {item}" for item in lista)
        + "\n\nEntre no CarbonLedger, em Coleta, para lançar os dados e anexar as evidências.\n"
    )
    return titulos[tipo], corpo


async def _enviar(
    conexao: Any,
    tenant_id: UUID,
    campanha: Campanha,
    tipo: str,
    enviar: Enviar,
    faltando: list[str],
) -> int:
    dados = await conexao.fetchrow(
        "SELECT e.nome, i.ano FROM inventario i JOIN empresa e"
        " ON e.tenant_id = i.tenant_id AND e.empresa_id = i.empresa_id"
        " WHERE i.inventario_id = $1",
        campanha.inventario_id,
    )
    para = await destinatarios(conexao, campanha.empresa_id)
    assunto, corpo = _texto(
        tipo, dados["nome"], dados["ano"], campanha.prazo, campanha.mensagem, faltando
    )
    # O registro vem antes do envio: se outro processo ja enviou este tipo, a chave unica
    # recusa e ninguem recebe aviso repetido.
    gravado = await conexao.fetchval(
        "INSERT INTO campanha_envio (tenant_id, campanha_id, tipo, empresa_id, destinatarios)"
        " VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING RETURNING tipo",
        tenant_id,
        campanha.campanha_id,
        tipo,
        campanha.empresa_id,
        len(para),
    )
    if gravado is None:
        return 0
    for email in para:
        await enviar(email, assunto, corpo)
    await audit.registrar_asyncpg(
        conexao,
        tenant_id=tenant_id,
        acao="campanha.aviso_enviado",
        entidade="campanha",
        entidade_id=campanha.campanha_id,
        dados={"empresa_id": str(campanha.empresa_id), "tipo": tipo, "destinatarios": len(para)},
    )
    return len(para)


async def abrir(
    conexao: Any,
    *,
    tenant_id: UUID,
    usuario_id: UUID,
    inventario_id: UUID,
    formulario: dict[str, str],
    hoje: date,
    enviar: Enviar,
) -> tuple[UUID, int]:
    inventario = await conexao.fetchrow(
        "SELECT empresa_id, estado FROM inventario WHERE inventario_id = $1", inventario_id
    )
    if inventario is None:
        raise AcessoNegado
    coletor = ColetorDeErros()
    if inventario["estado"] != "em_coleta":
        coletor.adicionar("prazo", "O inventário não está em coleta.")
    prazo = None
    try:
        prazo = date.fromisoformat((formulario.get("prazo") or "").strip())
        if prazo <= hoje:
            coletor.adicionar("prazo", "O prazo precisa ser depois de hoje.")
    except ValueError:
        coletor.adicionar("prazo", "Informe o prazo da coleta.")
    mensagem = (formulario.get("mensagem") or "").strip() or None
    if mensagem and len(mensagem) > TAMANHO_MAXIMO_MENSAGEM:
        coletor.adicionar("mensagem", f"Use no máximo {TAMANHO_MAXIMO_MENSAGEM} caracteres.")
    if await aberta(conexao, inventario_id) is not None:
        coletor.adicionar("prazo", "Já há uma campanha aberta para este inventário.")
    coletor.levantar_se_houver()
    if prazo is None:  # ja recusado; so para o tipo
        raise ErroValidacao("prazo", "Informe o prazo da coleta.")
    campanha = Campanha(
        uuid4(), inventario["empresa_id"], inventario_id, prazo, mensagem, None, None
    )
    await conexao.execute(
        "INSERT INTO campanha (tenant_id, campanha_id, empresa_id, inventario_id, prazo, mensagem,"
        " criada_por) VALUES ($1, $2, $3, $4, $5, $6, $7)",
        tenant_id,
        campanha.campanha_id,
        campanha.empresa_id,
        inventario_id,
        prazo,
        mensagem,
        usuario_id,
    )
    await audit.registrar_asyncpg(
        conexao,
        tenant_id=tenant_id,
        usuario_id=usuario_id,
        acao="campanha.aberta",
        entidade="campanha",
        entidade_id=campanha.campanha_id,
        dados={"empresa_id": str(campanha.empresa_id), "prazo": prazo.isoformat()},
    )
    avisados = await _enviar(
        conexao, tenant_id, campanha, "abertura", enviar, await pendencias(conexao, inventario_id)
    )
    return campanha.campanha_id, avisados


async def encerrar(
    conexao: Any, *, tenant_id: UUID, usuario_id: UUID | None, campanha_id: UUID, motivo: str
) -> None:
    linha = await conexao.fetchrow(
        "UPDATE campanha SET encerrada_em = now(), motivo_encerramento = $2"
        " WHERE campanha_id = $1 AND encerrada_em IS NULL RETURNING empresa_id",
        campanha_id,
        motivo,
    )
    if linha is None:
        raise AcessoNegado
    await audit.registrar_asyncpg(
        conexao,
        tenant_id=tenant_id,
        usuario_id=usuario_id,
        acao="campanha.encerrada",
        entidade="campanha",
        entidade_id=campanha_id,
        dados={"empresa_id": str(linha["empresa_id"]), "motivo": motivo},
    )


def tipo_do_lembrete(prazo: date, hoje: date) -> str | None:
    """O lembrete do dia: vencida depois do prazo; senao o menor marco ja alcancado."""
    dias = (prazo - hoje).days
    if dias < 0:
        return "vencida"
    for limite, tipo in reversed(LEMBRETES):
        if dias <= limite:
            return tipo
    return None


async def lembrar(conexao: Any, tenant_id: UUID, hoje: date, enviar: Enviar) -> int:
    """Rodada diaria de um tenant: encerra o que acabou e manda o lembrete devido. Devolve
    quantos e-mails sairam."""
    total = 0
    for linha in await conexao.fetch(
        "SELECT c.campanha_id, c.empresa_id, c.inventario_id, c.prazo, c.mensagem,"
        " c.encerrada_em, c.motivo_encerramento, i.estado FROM campanha c JOIN inventario i"
        "   ON i.tenant_id = c.tenant_id AND i.inventario_id = c.inventario_id"
        " WHERE c.encerrada_em IS NULL"
    ):
        dados = dict(linha)
        estado = dados.pop("estado")
        campanha = Campanha(**dados)
        if estado != "em_coleta":
            await encerrar(
                conexao,
                tenant_id=tenant_id,
                usuario_id=None,
                campanha_id=campanha.campanha_id,
                motivo="inventario_fechado",
            )
            continue
        faltando = await pendencias(conexao, campanha.inventario_id)
        if not faltando:
            await encerrar(
                conexao,
                tenant_id=tenant_id,
                usuario_id=None,
                campanha_id=campanha.campanha_id,
                motivo="coleta_completa",
            )
            continue
        tipo = tipo_do_lembrete(campanha.prazo, hoje)
        if tipo is not None:
            total += await _enviar(conexao, tenant_id, campanha, tipo, enviar, faltando)
    return total
