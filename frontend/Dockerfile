FROM ghcr.io/astral-sh/uv:0.9.18 AS uv

FROM python:3.12-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM builder AS test-builder
RUN uv sync --frozen --no-install-project

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --create-home app
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 app.py config.py ./
COPY --chown=10001:10001 clients ./clients
COPY --chown=10001:10001 components ./components
COPY --chown=10001:10001 state ./state
COPY --chown=10001:10001 views ./views
COPY --chown=10001:10001 utils ./utils
COPY --chown=10001:10001 .streamlit ./.streamlit
USER 10001:10001
EXPOSE 8501
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3)"
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]

FROM runtime AS test
ENV RUFF_CACHE_DIR=/tmp/ruff-cache \
    PYTEST_ADDOPTS="-o cache_dir=/tmp/pytest-cache"
COPY --from=test-builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 pyproject.toml ./
COPY --chown=10001:10001 tests ./tests
HEALTHCHECK NONE
CMD ["sh", "-c", "ruff check . && ruff format --check . && pytest -q"]
