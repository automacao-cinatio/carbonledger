"""Sugestoes da IA: categoria e atividade de uma fonte, e opiniao sobre um dado fora do padrao.

A IA so sugere. A sugestao de fonte so pode escolher entre as categorias do catalogo e as
atividades com fator aprovado (o esquema restringe as opcoes); a pessoa escolhe no formulario
e salva. A opiniao sobre o dado e texto para quem aprova; nao aprova nem devolve nada.

Vai para a IA so o necessario: descricao e unidade da fonte, e numeros dos periodos. Nenhum
nome, e-mail ou documento.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.modules.ia.cliente import ClienteIA, IAIndisponivel

CONFIANCAS = ("baixa", "media", "alta")
TAMANHO_MAXIMO_TEXTO = 400

SISTEMA_FONTE = (
    "Você ajuda empresas brasileiras a montar o inventário de emissões de gases de efeito estufa"
    " no padrão do Programa Brasileiro GHG Protocol. Recebe a descrição de uma fonte de emissão e"
    " a unidade em que o dado é medido, e escolhe a categoria mais provável entre as opções"
    " dadas e, se alguma servir, a atividade (combustível ou tipo de atividade) entre as opções"
    " dadas. Se nenhuma atividade servir, devolva atividade vazia. Diga a confiança e explique"
    " em uma frase curta, em português do Brasil. É só uma sugestão: a pessoa decide."
)
SISTEMA_DADO = (
    "Você ajuda quem aprova os dados de um inventário de emissões a notar valores fora do padrão."
    " Recebe a descrição da fonte, a unidade e os valores dos outros períodos, e um valor novo."
    " Diga se o valor novo parece fora do padrão da série e explique em até duas frases curtas,"
    " em português do Brasil, o que a pessoa deveria conferir (por exemplo, a unidade, uma"
    " vírgula fora do lugar ou a nota fiscal). Não aprove nem reprove: só aponte."
)


@dataclass(frozen=True)
class SugestaoFonte:
    categoria: str
    atividade: str | None
    confianca: str
    justificativa: str


@dataclass(frozen=True)
class OpiniaoDado:
    fora_do_padrao: bool
    explicacao: str


def _texto(dados: dict[str, Any], chave: str) -> str:
    valor = dados.get(chave)
    if not isinstance(valor, str):
        raise IAIndisponivel(f"campo {chave} fora do esquema")
    return valor.strip()[:TAMANHO_MAXIMO_TEXTO]


async def sugerir_fonte(
    ia: ClienteIA,
    *,
    descricao: str,
    unidade_medida: str,
    categorias: dict[str, str],
    atividades: dict[str, str],
) -> SugestaoFonte:
    esquema = {
        "type": "object",
        "properties": {
            "categoria": {"type": "string", "enum": sorted(categorias)},
            "atividade": {"type": "string", "enum": ["", *sorted(atividades)]},
            "confianca": {"type": "string", "enum": list(CONFIANCAS)},
            "justificativa": {"type": "string"},
        },
        "required": ["categoria", "atividade", "confianca", "justificativa"],
        "additionalProperties": False,
    }
    opcoes_categoria = "\n".join(f"- {c}: {r}" for c, r in sorted(categorias.items()))
    opcoes_atividade = (
        "\n".join(f"- {c}: {r}" for c, r in sorted(atividades.items())) or "(nenhuma)"
    )
    conteudo = (
        f"Descrição da fonte: {descricao.strip()[:200]}\n"
        f"Unidade do dado: {unidade_medida.strip()[:40] or 'não informada'}\n\n"
        f"Categorias possíveis:\n{opcoes_categoria}\n\n"
        f"Atividades possíveis:\n{opcoes_atividade}"
    )
    dados = await ia.gerar(SISTEMA_FONTE, conteudo, esquema)
    categoria = _texto(dados, "categoria")
    atividade = _texto(dados, "atividade") or None
    confianca = _texto(dados, "confianca")
    # O esquema ja restringe; conferimos de novo para nunca mostrar opcao que nao existe.
    if categoria not in categorias or (atividade is not None and atividade not in atividades):
        raise IAIndisponivel("opcao fora da lista")
    if confianca not in CONFIANCAS:
        raise IAIndisponivel("confianca fora da lista")
    return SugestaoFonte(categoria, atividade, confianca, _texto(dados, "justificativa"))


def _numero(valor: Decimal) -> str:
    normalizado = valor.normalize()
    return format(normalizado, "f") if normalizado != 0 else "0"


async def opinar_sobre_dado(
    ia: ClienteIA,
    *,
    descricao: str,
    unidade: str,
    historico: list[tuple[str, Decimal]],
    novo: tuple[str, Decimal],
) -> OpiniaoDado:
    esquema = {
        "type": "object",
        "properties": {
            "fora_do_padrao": {"type": "boolean"},
            "explicacao": {"type": "string"},
        },
        "required": ["fora_do_padrao", "explicacao"],
        "additionalProperties": False,
    }
    serie = "\n".join(f"- {periodo}: {_numero(q)} {unidade}" for periodo, q in historico)
    conteudo = (
        f"Fonte: {descricao.strip()[:200]}\nUnidade: {unidade}\n\n"
        f"Outros períodos:\n{serie or '(nenhum)'}\n\n"
        f"Valor novo, em {novo[0]}: {_numero(novo[1])} {unidade}"
    )
    dados = await ia.gerar(SISTEMA_DADO, conteudo, esquema)
    fora = dados.get("fora_do_padrao")
    if not isinstance(fora, bool):
        raise IAIndisponivel("campo fora_do_padrao fora do esquema")
    return OpiniaoDado(fora, _texto(dados, "explicacao"))
