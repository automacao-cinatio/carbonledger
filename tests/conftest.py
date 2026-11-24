import os

# Valores falsos, so para os testes importarem o app; nenhum servico real e usado.
os.environ.setdefault("DATABASE_URL", "postgresql://teste:teste@localhost:5432/teste")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("S3_ENDPOINT", "http://localhost:9000")
os.environ.setdefault("S3_ACCESS_KEY", "teste")
os.environ.setdefault("S3_SECRET_KEY", "teste")

# Os testes nao leem o .env local: so as variaveis acima e as que cada teste define.
from app.core.config import Configuracao

Configuracao.model_config["env_file"] = None
