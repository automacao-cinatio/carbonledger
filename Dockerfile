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

COPY app ./app
COPY migrations ./migrations

RUN useradd --system --uid 10001 --home-dir /app appuser \
    && chown -R appuser /app
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
