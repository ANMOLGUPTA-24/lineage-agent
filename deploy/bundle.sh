#!/bin/sh
# Assemble build/space/: what gets uploaded to the Hugging Face Space.
#   sh deploy/bundle.sh [falkordb-container]      (default: lineage-falkordb)
set -e
C=${1:-lineage-falkordb}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT="$ROOT/build/space"
rm -rf "$OUT" && mkdir -p "$OUT"
docker exec "$C" redis-cli SAVE >/dev/null
docker cp "$C:/var/lib/falkordb/data/dump.rdb" "$OUT/graph.rdb"
cp -r "$ROOT/lineage" "$OUT/lineage"
find "$OUT/lineage" -name __pycache__ -prune -exec rm -rf {} +
cp "$ROOT/pyproject.toml" "$ROOT/uv.lock" "$ROOT/.python-version" "$OUT/"
cp "$ROOT/deploy/Dockerfile" "$ROOT/deploy/start.sh" "$OUT/"
cat > "$OUT/README.md" <<'MD'
---
title: Lineage
emoji: 🐍
colorFrom: blue
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
short_description: Has Python already decided this? A FalkorDB graph agent.
---

Live demo of [Lineage](https://github.com/ANMOLGUPTA-24/lineage-agent): an agent that answers
questions about Python's 25 years of PEP decisions by walking a FalkorDB graph.
Source, graph model, queries and evaluation are in the GitHub repo.
MD
du -sh "$OUT" "$OUT/graph.rdb"
