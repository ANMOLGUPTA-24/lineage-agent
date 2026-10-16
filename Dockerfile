FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git make && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
# Bake the embedding model into the image so the first question isn't slow.
RUN uv run python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"
ENV PORT=8000
CMD uv run uvicorn lineage.server:app --host 0.0.0.0 --port $PORT
