"""Web app: one page, one JSON endpoint.  uv run uvicorn lineage.server:app --port 8000"""
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from lineage import agent
from lineage.db import connect

app = FastAPI(title="Lineage")
WEB = Path(__file__).parent / "web"


class Ask(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    session: str | None = Field(default=None, max_length=32)
    tenant: str = Field(default="python", max_length=40)


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "graphs": connect().list_graphs()}


@app.post("/api/ask")
def ask(body: Ask):
    if not re.fullmatch(r"[a-z0-9_]+", body.tenant):
        raise HTTPException(400, "bad tenant name")
    return agent.ask(body.question, tenant=body.tenant, session=body.session)
