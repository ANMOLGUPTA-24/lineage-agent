# Lineage: run `make ingest` once, then `make app`.
PY = uv run python

data/raw/peps:
	git clone --depth 1 https://github.com/python/peps data/raw/peps

fetch:            ## refresh the cached public data (about 10 min; needs `gh auth login`)
	curl -sL https://peps.python.org/api/peps.json -o data/raw/peps.json
	$(PY) -m lineage.ingest.github fetch

ingest: data/raw/peps   ## build the graph from data/raw
	$(PY) -m lineage.ingest.peps
	$(PY) -m lineage.ingest.github load
	$(PY) -m lineage.ingest.derive
	$(PY) -m lineage.ingest.sections
	$(PY) -m lineage.ingest.expertise

app:
	uv run uvicorn lineage.server:app --port 8000

mcp:
	$(PY) -m lineage.mcp_server

test:
	uv run pytest -q

.PHONY: fetch ingest app mcp test
