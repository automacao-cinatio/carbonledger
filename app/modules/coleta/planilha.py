"""Planilha de coleta em CSV: modelo preenchido por inventario e importacao.

Formato (descrito em docs/runbooks/coleta-e-evidencias.md): CSV em UTF-8, separador ";",
decimal com virgula, uma linha por fonte e periodo. As colunas fonte_id e periodo amarram a
linha; quantidade, unidade_medida, data_compra e observacao sao lidas; as demais so ajudam a
pessoa a se achar. Linha sem quantidade e ignorada; linha igual ao dado atual tambem.

Importacao tudo ou nada: com qualquer erro, nada e gravado, e todos os erros saem juntos, com
linha e coluna.
"""

import csv
import io
from dataclasses import dataclass
from datetime import date
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.antivirus import Antivirus
from app.core.armazenamento import ArmazemObjetos
from app.core.arquivos import CSV, TXT
from app.core.csv_seguro import SEPARADOR, gerar_csv
from app.core.erros import ColetorDeErros, ErrosDeValidacao, ErroValidacao
from app.core.security.acesso import Acesso
from app.modules.coleta import servico
from app.modules.coleta.categorias import periodos, rotulo, rotulo_periodo
from app.modules.coleta.evidencias import chave_objeto, conferir_e_verificar
from app.web.formatos import formatar_data, formatar_numero

COLUNAS = (
    "fonte_id",
    "unidade",
    "categoria",
    "descricao",
    "periodo",
    "nome_periodo",
    "quantidade",
    "unidade_medida",
    "data_compra",
    "observacao",
)
OBRIGATORIAS = ("fonte_id", "periodo", "quantidade", "unidade_medida", "data_compra", "observacao")
MAXIMO_LINHAS = 5000
MAXIMO_ERROS = 50


def _texto_periodo(periodo: int) -> str:
    return "ano" if periodo == 0 else f"{periodo:02d}"


def _ler_periodo(texto: str) -> int | None:
    limpo = texto.strip().lower()
    if limpo == "ano":
        return 0
    if limpo.isdigit() and 1 <= int(limpo) <= 12:
        return int(limpo)
    return None


def _quantidade_texto(quantidade: object) -> str:
    # Decimal sem zeros sobrando e sem separador de milhar, para reler sem ambiguidade.
    if quantidade is None:
        return ""
    texto = formatar_numero(quantidade, casas=servico.CASAS_MAXIMAS)  # type: ignore[arg-type]
    texto = texto.replace(".", "")
    return texto.rstrip("0").rstrip(",") if "," in texto else texto


async def gerar_modelo(sessao: AsyncSession, inventario_id: UUID) -> bytes:
    situacoes = await servico.situacao_das_fontes(sessao, inventario_id)
    linhas = []
    for situacao in situacoes:
        fonte = situacao.fonte
        if not fonte.ativa:
            continue
        for periodo in periodos(fonte.periodicidade):
            atual = situacao.atuais.get(periodo)
            linhas.append(
                (
                    fonte.fonte_id,
                    fonte.unidade_nome,
                    rotulo(fonte.categoria),
                    fonte.descricao,
                    _texto_periodo(periodo),
                    rotulo_periodo(periodo),
                    _quantidade_texto(atual.quantidade) if atual else "",
                    atual.unidade if atual else fonte.unidade_medida,
                    formatar_data(atual.data_compra) if atual else "",
                    (atual.observacao or "") if atual else "",
                )
            )
    return gerar_csv(COLUNAS, linhas)


@dataclass(frozen=True)
class LinhaLida:
    numero: int
    fonte: servico.Fonte
    novo: servico.NovoLancamento


def _decodificar(conteudo: bytes) -> str:
    try:
        return conteudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        # O Excel em pt-BR salva "CSV (separado por virgulas)" em Windows-1252.
        return conteudo.decode("cp1252")
    except UnicodeDecodeError as erro:
        raise ErroValidacao(
            "arquivo", "Salve a planilha como CSV UTF-8 (separado por ponto e vírgula)."
        ) from erro


def _celula(valor: str) -> str:
    """Desfaz a neutralizacao de formula do modelo (apostrofo na frente)."""
    limpo = valor.strip()
    return limpo[1:] if limpo.startswith("'") else limpo


async def ler_planilha(
    sessao: AsyncSession, inventario: servico.Inventario, conteudo: bytes, hoje: date
) -> list[LinhaLida]:
    texto = _decodificar(conteudo)
    leitor = csv.reader(io.StringIO(texto), delimiter=SEPARADOR)
    cabecalho = [c.strip().lower() for c in next(leitor, [])]
    faltando = [c for c in OBRIGATORIAS if c not in cabecalho]
    if faltando:
        raise ErroValidacao(
            "arquivo",
            f"Faltam as colunas {', '.join(faltando)}. Baixe o modelo de novo e use-o como base.",
        )
    posicao = {nome: cabecalho.index(nome) for nome in OBRIGATORIAS}
    fontes = {f.fonte_id: f for f in await servico.listar_fontes(sessao, inventario.inventario_id)}
    problemas: list[str] = []
    lidas: list[LinhaLida] = []
    vistos: set[tuple[UUID, int]] = set()

    def anotar(numero: int, coluna: str, mensagem: str) -> None:
        problemas.append(f"Linha {numero}, coluna {coluna}: {mensagem}")

    for numero, linha in enumerate(leitor, start=2):
        if numero - 1 > MAXIMO_LINHAS:
            raise ErroValidacao("arquivo", f"A planilha passa de {MAXIMO_LINHAS} linhas.")
        if not any(c.strip() for c in linha):
            continue
        valores = {nome: _celula(linha[i]) if i < len(linha) else "" for nome, i in posicao.items()}
        if not valores["quantidade"]:
            continue
        try:
            fonte = fontes.get(UUID(valores["fonte_id"]))
        except ValueError:
            fonte = None
        if fonte is None:
            anotar(
                numero, "fonte_id", "fonte não encontrada neste inventário. Não mude essa coluna."
            )
            continue
        if not fonte.ativa:
            anotar(numero, "fonte_id", "a fonte está desativada. Reative-a ou apague a linha.")
            continue
        periodo = _ler_periodo(valores["periodo"])
        if periodo is None or periodo not in periodos(fonte.periodicidade):
            anotar(numero, "periodo", "use 01 a 12 (fonte mensal) ou ano (fonte anual).")
            continue
        if (fonte.fonte_id, periodo) in vistos:
            anotar(numero, "periodo", "este período da fonte aparece mais de uma vez.")
            continue
        vistos.add((fonte.fonte_id, periodo))
        try:
            novo = servico.validar_lancamento(
                fonte,
                inventario,
                {
                    "periodo": str(periodo),
                    "quantidade": valores["quantidade"],
                    "unidade": valores["unidade_medida"],
                    "data_compra": valores["data_compra"],
                    "observacao": valores["observacao"],
                },
                hoje,
            )
        except ErrosDeValidacao as erros:
            for erro in erros.erros:
                coluna = "unidade_medida" if erro.campo == "unidade" else erro.campo
                anotar(numero, coluna, erro.mensagem)
            continue
        lidas.append(LinhaLida(numero, fonte, novo))

    if problemas:
        coletor = ColetorDeErros()
        resumo = problemas[:MAXIMO_ERROS]
        if len(problemas) > MAXIMO_ERROS:
            resumo.append(f"E mais {len(problemas) - MAXIMO_ERROS} problemas.")
        coletor.adicionar("arquivo", " ".join(resumo))
        coletor.levantar_se_houver()
    return lidas


async def importar_planilha(
    sessao: AsyncSession,
    acesso: Acesso,
    inventario_id: UUID,
    nome: str | None,
    conteudo: bytes,
    *,
    armazem: ArmazemObjetos,
    antivirus: Antivirus,
    limite_bytes: int,
    hoje: date,
) -> int:
    """Grava as linhas novas ou alteradas. Devolve quantas versoes foram gravadas."""
    inventario = await servico.obter_inventario(sessao, inventario_id)
    if not inventario.em_coleta:
        raise ErroValidacao("arquivo", "O inventário não está em coleta.")
    arquivo = await conferir_e_verificar(
        nome,
        conteudo,
        limite_bytes=limite_bytes,
        antivirus=antivirus,
        campo="arquivo",
        aceitos=(CSV, TXT),
        descricao_aceitos="a planilha em CSV (separado por ponto e vírgula)",
    )
    lidas = await ler_planilha(sessao, inventario, arquivo.conteudo, hoje)

    planilha_id = uuid4()
    guardado = await armazem.guardar(
        chave_objeto(acesso.tenant_id, inventario.empresa_id, "planilhas", planilha_id),
        arquivo.conteudo,
        arquivo.sha256,
        arquivo.tipo.tipo_conteudo,
    )
    # O registro da planilha entra antes dos lancamentos (chave estrangeira); o total e
    # contado antes, conferindo quais linhas mudam algo.
    # Versoes atuais de todas as fontes numa consulta so (antes, duas por linha).
    atuais = await servico.versoes_atuais(sessao, list({lida.fonte.fonte_id for lida in lidas}))
    mudam = [
        lida
        for lida in lidas
        if not servico.igual_ao_atual(
            atuais.get((lida.fonte.fonte_id, lida.novo.periodo)), lida.novo
        )
    ]
    await sessao.execute(
        text(
            "INSERT INTO planilha_importacao (tenant_id, planilha_id, empresa_id, inventario_id,"
            " nome_arquivo, sha256, chave_objeto, linhas_gravadas, enviado_por)"
            " VALUES (:tenant, :id, :empresa, :inventario, :nome, :sha256, :chave, :linhas,"
            " :usuario)"
        ),
        {
            "tenant": acesso.tenant_id,
            "id": planilha_id,
            "empresa": inventario.empresa_id,
            "inventario": inventario_id,
            "nome": arquivo.nome,
            "sha256": arquivo.sha256,
            "chave": guardado.chave,
            "linhas": len(mudam),
            "usuario": acesso.usuario_id,
        },
    )
    for lida in mudam:
        await servico.gravar_versao(
            sessao,
            acesso,
            lida.fonte,
            lida.novo,
            atuais.get((lida.fonte.fonte_id, lida.novo.periodo)),
            origem="planilha",
            planilha_id=planilha_id,
        )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="planilha.importada",
        entidade="planilha_importacao",
        entidade_id=planilha_id,
        dados={
            "empresa_id": str(inventario.empresa_id),
            "inventario_id": str(inventario_id),
            "linhas_gravadas": len(mudam),
            "sha256": arquivo.sha256.hex(),
        },
    )
    return len(mudam)
