"""Evidencias da coleta: conferencia do arquivo, antivirus, S3 com Object Lock e registro.

Ordem: tipo pelo conteudo e tamanho, depois ClamAV, depois S3, e so entao o banco. Se o banco
falhar depois do S3, sobra um objeto sem registro (retido pelo Object Lock), nunca um registro
sem objeto. O nome do arquivo fica so no banco; a chave do objeto leva identificadores.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.antivirus import Antivirus, AntivirusIndisponivel, ArquivoInfectado
from app.core.armazenamento import ArmazemObjetos
from app.core.arquivos import ArquivoRecusado, TipoArquivo, conferir_arquivo
from app.core.erros import AcessoNegado, ErroValidacao
from app.core.security.acesso import Acesso
from app.modules.coleta.categorias import periodos
from app.modules.coleta.servico import obter_fonte, obter_inventario


@dataclass(frozen=True)
class Evidencia:
    evidencia_id: UUID
    fonte_id: UUID
    periodo: int | None
    nome_arquivo: str
    tipo_conteudo: str
    tamanho: int
    sha256: bytes
    enviado_em: datetime

    @property
    def sha256_hex(self) -> str:
        return self.sha256.hex()


@dataclass(frozen=True)
class ArquivoConferido:
    nome: str
    tipo: TipoArquivo
    conteudo: bytes
    sha256: bytes


async def conferir_e_verificar(
    nome: str | None,
    conteudo: bytes,
    *,
    limite_bytes: int,
    antivirus: Antivirus,
    campo: str,
    aceitos: tuple[TipoArquivo, ...] | None = None,
    descricao_aceitos: str | None = None,
) -> ArquivoConferido:
    """Tipo, tamanho e antivirus. Qualquer recusa vira erro do campo, com como corrigir."""
    try:
        if aceitos is not None and descricao_aceitos is not None:
            limpo, tipo = conferir_arquivo(
                nome,
                conteudo,
                limite_bytes=limite_bytes,
                aceitos=aceitos,
                descricao_aceitos=descricao_aceitos,
            )
        else:
            limpo, tipo = conferir_arquivo(nome, conteudo, limite_bytes=limite_bytes)
    except ArquivoRecusado as erro:
        raise ErroValidacao(campo, str(erro)) from erro
    try:
        await antivirus.verificar(conteudo)
    except ArquivoInfectado as erro:
        raise ErroValidacao(
            campo, "O antivírus encontrou uma ameaça neste arquivo. Ele não foi guardado."
        ) from erro
    except AntivirusIndisponivel as erro:
        raise ErroValidacao(
            campo,
            "Não foi possível verificar o arquivo agora. Tente de novo em alguns minutos.",
        ) from erro
    return ArquivoConferido(limpo, tipo, conteudo, hashlib.sha256(conteudo).digest())


def chave_objeto(tenant_id: UUID, empresa_id: UUID, pasta: str, objeto_id: UUID) -> str:
    return f"tenants/{tenant_id}/empresas/{empresa_id}/{pasta}/{objeto_id}"


async def enviar_evidencia(
    sessao: AsyncSession,
    acesso: Acesso,
    fonte_id: UUID,
    periodo_texto: str,
    nome: str | None,
    conteudo: bytes,
    *,
    armazem: ArmazemObjetos,
    antivirus: Antivirus,
    limite_bytes: int,
) -> UUID:
    fonte = await obter_fonte(sessao, fonte_id)
    inventario = await obter_inventario(sessao, fonte.inventario_id)
    if not inventario.em_coleta:
        raise ErroValidacao("arquivo", "O inventário não está em coleta.")
    limpo = periodo_texto.strip()
    periodo: int | None = None
    if limpo:
        periodo = int(limpo) if limpo.isdigit() else -1
        if periodo not in periodos(fonte.periodicidade):
            raise ErroValidacao("periodo", "Escolha o período da evidência.")
    arquivo = await conferir_e_verificar(
        nome, conteudo, limite_bytes=limite_bytes, antivirus=antivirus, campo="arquivo"
    )
    evidencia_id = uuid4()
    guardado = await armazem.guardar(
        chave_objeto(acesso.tenant_id, fonte.empresa_id, "evidencias", evidencia_id),
        arquivo.conteudo,
        arquivo.sha256,
        arquivo.tipo.tipo_conteudo,
    )
    await sessao.execute(
        text(
            "INSERT INTO evidencia (tenant_id, evidencia_id, empresa_id, fonte_id, periodo,"
            " nome_arquivo, tipo_conteudo, tamanho, sha256, chave_objeto, versao_objeto,"
            " enviado_por) VALUES (:tenant, :id, :empresa, :fonte, :periodo, :nome, :tipo,"
            " :tamanho, :sha256, :chave, :versao, :usuario)"
        ),
        {
            "tenant": acesso.tenant_id,
            "id": evidencia_id,
            "empresa": fonte.empresa_id,
            "fonte": fonte_id,
            "periodo": periodo,
            "nome": arquivo.nome,
            "tipo": arquivo.tipo.tipo_conteudo,
            "tamanho": len(arquivo.conteudo),
            "sha256": arquivo.sha256,
            "chave": guardado.chave,
            "versao": guardado.versao,
            "usuario": acesso.usuario_id,
        },
    )
    await audit.registrar(
        sessao,
        tenant_id=acesso.tenant_id,
        usuario_id=acesso.usuario_id,
        acao="evidencia.enviada",
        entidade="evidencia",
        entidade_id=evidencia_id,
        dados={
            "empresa_id": str(fonte.empresa_id),
            "fonte_id": str(fonte_id),
            "periodo": periodo,
            "sha256": arquivo.sha256.hex(),
            "tamanho": len(arquivo.conteudo),
        },
    )
    return evidencia_id


async def listar_evidencias(sessao: AsyncSession, fonte_id: UUID) -> list[Evidencia]:
    linhas = await sessao.execute(
        text(
            "SELECT evidencia_id, fonte_id, periodo, nome_arquivo, tipo_conteudo, tamanho,"
            " sha256, enviado_em FROM evidencia WHERE fonte_id = :fonte"
            " ORDER BY periodo NULLS FIRST, enviado_em DESC"
        ),
        {"fonte": fonte_id},
    )
    return [Evidencia(**linha._asdict()) for linha in linhas]


async def ler_evidencia(
    sessao: AsyncSession, evidencia_id: UUID, armazem: ArmazemObjetos
) -> tuple[Evidencia, bytes]:
    linha = (
        await sessao.execute(
            text(
                "SELECT evidencia_id, fonte_id, periodo, nome_arquivo, tipo_conteudo, tamanho,"
                " sha256, enviado_em, chave_objeto, versao_objeto FROM evidencia"
                " WHERE evidencia_id = :id"
            ),
            {"id": evidencia_id},
        )
    ).first()
    if linha is None:
        raise AcessoNegado
    dados = linha._asdict()
    chave, versao = dados.pop("chave_objeto"), dados.pop("versao_objeto")
    evidencia = Evidencia(**dados)
    conteudo = await armazem.ler(chave, versao)
    # O arquivo so sai se ainda for o mesmo que entrou.
    if hashlib.sha256(conteudo).digest() != evidencia.sha256:
        raise RuntimeError("evidencia com hash diferente do registrado")
    return evidencia, conteudo
