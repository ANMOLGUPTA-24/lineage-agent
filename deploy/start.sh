#!/bin/sh
# One container: FalkorDB on localhost only (no Browser, no outside access), then the app.
set -e
DATA=/tmp/falkordb
mkdir -p "$DATA"
[ -f "$DATA/dump.rdb" ] || cp /opt/lineage/graph.rdb "$DATA/dump.rdb"
redis-server --bind 127.0.0.1 --port 6379 --dir "$DATA" --dbfilename dump.rdb --save "" \
    --loadmodule /var/lib/falkordb/bin/falkordb.so --daemonize yes
until redis-cli ping >/dev/null 2>&1; do sleep 0.2; done
echo "FalkorDB up: $(redis-cli GRAPH.LIST | tr '\n' ' ')"
exec /app/.venv/bin/uvicorn lineage.server:app --host 0.0.0.0 --port "${PORT:-7860}"
