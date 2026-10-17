"""Web app: one page, one JSON endpoint.  uv run uvicorn lineage.server:app --port 8000"""
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
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


@app.get("/api/health")
def health():
    last = graph().ro_query("MATCH (s:Sync) RETURN s.at, s.changes ORDER BY s.at DESC LIMIT 1").result_set
    return {"ok": True, "live_graph": resolve("python"), "graphs": connect().list_graphs(),
            "last_sync": {"at": last[0][0], "changes": last[0][1]} if last else None}


@app.post("/api/ask")
def ask(body: Ask):
    if not re.fullmatch(r"[a-z0-9_]+", body.tenant):
        raise HTTPException(400, "bad tenant name")
    return agent.ask(body.question, tenant=body.tenant, session=body.session)
