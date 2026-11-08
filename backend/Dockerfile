FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM builder AS test-builder
RUN uv sync --frozen --no-install-project

FROM base AS runtime
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app alembic.ini pyproject.toml ./
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--ws-max-size", "4096", "--timeout-keep-alive", "15", "--timeout-graceful-shutdown", "15"]

FROM runtime AS test
COPY --from=test-builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app tests ./tests
ENV PYTEST_ADDOPTS="-p no:cacheprovider"
CMD ["sh", "-c", "ruff check --no-cache app tests migrations && pytest -q"]
