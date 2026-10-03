FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8000
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY meldx ./meldx
COPY alembic ./alembic
COPY alembic.ini ./
RUN uv sync --locked --no-dev
# Migration failure prevents serving an incompatible schema. One worker shares SSE.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn meldx.app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
