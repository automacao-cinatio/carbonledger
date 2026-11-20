"""Utilitarios de conexao com o banco."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def dsn_asyncpg(database_url: str) -> str:
    """Prepara a URL para asyncpg.connect().

    Remove o prefixo de driver do SQLAlchemy e o parametro ssl=disable, que o asyncpg
    nao aceita na DSN. Em producao o TLS ate o banco e obrigatorio.
    """
    partes = urlsplit(database_url)
    esquema = partes.scheme.split("+", 1)[0]
    consulta = [(k, v) for k, v in parse_qsl(partes.query) if not (k == "ssl" and v == "disable")]
    return urlunsplit((esquema, partes.netloc, partes.path, urlencode(consulta), partes.fragment))
