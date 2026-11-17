FROM python:3.12.9-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --extra app
COPY src ./src
RUN uv sync --frozen --extra app
ENV PATH=/app/.venv/bin:$PATH
USER 65534:65534
CMD exec uvicorn judgebench.worker.app:app --host 0.0.0.0 --port ${PORT:-8080}
