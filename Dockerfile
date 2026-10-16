FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git make && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
# Bake both local models (embedder + reranker) into the image so the first question isn't slow.
RUN uv run python -c "from lineage import tools; list(tools._embedder().embed(['x'])); tools._reranker().rerank('x', ['y'])"
ENV PORT=8000
CMD uv run uvicorn lineage.server:app --host 0.0.0.0 --port $PORT
