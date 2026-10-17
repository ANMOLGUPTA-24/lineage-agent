"""Web app: one page, one JSON endpoint.  uv run uvicorn lineage.server:app --port 8000"""
import re
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from lineage import agent
from lineage.db import connect, graph, resolve

app = FastAPI(title="Lineage")
WEB = Path(__file__).parent / "web"


class Ask(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    session: str | None = Field(default=None, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    tenant: str = Field(default="python", max_length=40)


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/hero.json")
def hero():
    """The recorded answer the home page opens with (scripts/build_static_demo.py)."""
    return FileResponse(WEB / "hero.json")


@app.get("/api/health")
def health():
    last = graph().ro_query("MATCH (s:Sync) RETURN s.at, s.changes ORDER BY s.at DESC LIMIT 1").result_set
    return {"ok": True, "live_graph": resolve("python"), "graphs": connect().list_graphs(),
            "last_sync": {"at": last[0][0], "changes": last[0][1]} if last else None}


# Per-client sliding window. Every answer also writes session memory into the graph, so an
# unthrottled loop would both queue CPU work and grow the graph. In-process: one worker, no Redis needed.
RATE, WINDOW = 30, 60.0
_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def throttled(client: str) -> bool:
    now = time.monotonic()
    with _lock:
        q = _hits[client]
        while q and now - q[0] > WINDOW:
            q.popleft()
        if len(q) >= RATE:
            return True
        q.append(now)
        if len(_hits) > 10_000:   # forget idle clients
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > WINDOW]:
                del _hits[k]
        return False


@app.post("/api/ask")
def ask(body: Ask, request: Request):
    if throttled(request.client.host if request.client else "unknown"):
        raise HTTPException(429, f"Too many questions: at most {RATE} a minute. Try again shortly.")
    if not re.fullmatch(r"[a-z0-9_]+", body.tenant):
        raise HTTPException(400, "bad tenant name")
    return agent.ask(body.question, tenant=body.tenant, session=body.session)
