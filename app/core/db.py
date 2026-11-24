"""Utilitarios de conexao com o banco e base dos modelos SQLAlchemy."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# Nomes previsiveis para restricoes e indices: as migracoes escritas a mao dependem deles.
CONVENCAO_NOMES = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=CONVENCAO_NOMES)


def dsn_asyncpg(database_url: str) -> str:
    """Prepara a URL para asyncpg.connect().

    Remove o prefixo de driver do SQLAlchemy e o parametro ssl=disable, que o asyncpg
    nao aceita na DSN. Em producao o TLS ate o banco e obrigatorio.
    """
    partes = urlsplit(database_url)
    esquema = partes.scheme.split("+", 1)[0]
    consulta = [(k, v) for k, v in parse_qsl(partes.query) if not (k == "ssl" and v == "disable")]
    return urlunsplit((esquema, partes.netloc, partes.path, urlencode(consulta), partes.fragment))


def url_sqlalchemy_asyncpg(database_url: str) -> str:
    """URL para o SQLAlchemy com o driver asyncpg (postgresql+asyncpg://...)."""
    dsn = dsn_asyncpg(database_url)
    return "postgresql+asyncpg" + dsn[dsn.index(":") :]


def criar_motor(database_url: str) -> AsyncEngine:
    """Motor async da API; o pool desfaz a transacao (e o contexto) ao devolver a conexao."""
    return create_async_engine(
        url_sqlalchemy_asyncpg(database_url),
        pool_pre_ping=True,
        pool_reset_on_return="rollback",
    )
