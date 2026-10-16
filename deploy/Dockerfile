# Hugging Face Spaces (Docker SDK): FalkorDB + Lineage in one container, graph pre-built.
# Assembled by deploy/bundle.sh; graph.rdb is a SAVE of a graph built with `make ingest` / `make sync`.
FROM falkordb/falkordb@sha256:b0bdcac2cbaff989e77cb0610439b7a9289e86de033ccbbd4368b66fb680ae7a
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_LINK_MODE=copy UV_CACHE_DIR=/tmp/uv \
    FASTEMBED_CACHE_PATH=/opt/fastembed HF_HUB_DISABLE_TELEMETRY=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY lineage ./lineage
RUN uv sync --frozen --no-dev
# Both local models (embedder + reranker) baked in, so the first question needs no download.
RUN .venv/bin/python -c "from lineage import tools; list(tools._embedder().embed(['x'])); tools._reranker().rerank('x', ['y'])"
COPY graph.rdb /opt/lineage/graph.rdb
COPY start.sh /start.sh
# Spaces run as uid 1000: everything readable, model cache writable.
RUN chmod +x /start.sh && chmod -R a+rX /app /opt && chmod -R a+rwX /opt/fastembed
ENV PORT=7860
EXPOSE 7860
ENTRYPOINT ["/start.sh"]
