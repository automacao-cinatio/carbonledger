FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencias primeiro, para aproveitar o cache de camadas.
COPY pyproject.toml README.md ./
COPY uv.loc[k] ./
RUN uv sync --no-dev

# Chromium do Playwright para o relatorio em PDF (P19), numa pasta fora do usuario.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# libatomic1: o Node embutido no Playwright precisa dela e a imagem slim nao traz.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && uv run --no-dev playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

# Codigo e dependencias ficam do root: o usuario do app so le. O que precisa escrever vai
# para /tmp (o beat grava a agenda la; o Chromium, o perfil temporario).
RUN useradd --system --uid 10001 --home-dir /tmp appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
