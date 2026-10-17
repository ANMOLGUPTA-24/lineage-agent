"""Build docs/demo/: the real UI plus recorded answers, for a static live demo (GitHub Pages).

  uv run python scripts/build_static_demo.py

Every answer in the snapshot is a real response from the running graph (tools, rows, paths, Cypher),
recorded by calling the agent exactly as the web server does. The crawl starts from the questions the
home page offers and follows each answer's suggested next questions, so every button on the demo
leads to a recorded answer. Anything else shows how to run Lineage locally to ask it.
"""
import json
import re
import uuid
from datetime import date
from pathlib import Path

from lineage import agent
from lineage.db import graph

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "demo"
SEEDS = [
    "Has anyone proposed a do-while loop?",
    "Was there a PEP for None-aware operators (?. and ??)",
    "Draft a reply to someone proposing a switch statement",
    "Have multi-line lambdas ever been proposed?",
    "Why does Python have the walrus operator?",
    "Who should I ask about typing?",
    "How is PEP 484 related to PEP 695?",
    "Is PEP 333 still current?",
    "Has anyone proposed statement-local name bindings with a given clause?",
    "Has anyone proposed a frozendict built-in?",
    "Was there a PEP for keyword arguments in indexing like a[x=1]?",
    "Has anyone proposed a banana keyword?",
    "What changed recently?",
]


def norm(q: str) -> str:
    """Must match norm() in lineage/web/index.html."""
    return re.sub(r"[?!.]+$", "", re.sub(r"\s+", " ", q.strip().lower()))


def follow_ups(res: dict) -> list[str]:
    """Same rules as followUps() in lineage/web/index.html."""
    steps = res.get("steps") or []
    main = next((s for s in reversed(steps) if s["tool"] not in ("resolve", "recall", "session_link")), None)
    if not main or main["outcome"] != "answered":
        return []
    rows, out = main.get("rows") or [], []
    if main["tool"] == "already_decided":
        p = next((r for r in rows if isinstance(r.get("pep"), int) and r.get("kind") != "thread"), None)
        if p:
            out += [f"Tell me the history of PEP {p['pep']}", f"Who should I ask about PEP {p['pep']}?"]
        idea = main["args"].get("searched_for") or main["args"].get("idea")
        if idea:
            out.append(f"Draft a reply to someone proposing {idea}")
    elif main["tool"] == "why" and rows:
        out += [f"Is PEP {rows[0]['pep']} still current?", f"Who should I ask about PEP {rows[0]['pep']}?"]
    elif main["tool"] == "who_knows" and rows:
        out.append(f"What is the bus factor of {re.sub(r'^topic ', '', rows[0]['subject'])}?")
    elif main["tool"] == "is_current" and rows:
        out.append(f"Tell me the history of PEP {rows[0]['pep']}")
    return out[:3]


def main() -> None:
    answers, queue, seen = {}, [(q, 0) for q in SEEDS], set()
    sessions = []
    while queue:
        q, depth = queue.pop(0)
        if norm(q) in seen:
            continue
        seen.add(norm(q))
        sid = "snap" + uuid.uuid4().hex[:10]   # a fresh session per question: no memory links between them
        sessions.append(sid)
        res = agent.ask(q, session=sid)
        res.pop("session", None)
        answers[norm(q)] = res
        if depth < 2:
            queue += [(f, depth + 1) for f in follow_ups(res)]
    graph().query("MATCH (s:Session) WHERE s.id IN $ids OPTIONAL MATCH (s)-[:ASKED]->(q) DETACH DELETE q, s",
                  {"ids": sessions})
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "answers.json").write_text(json.dumps(answers, default=str))
    page = (ROOT / "lineage" / "web" / "index.html").read_text()
    flag = f'<script>window.LINEAGE_STATIC = {{date: "{date.today().isoformat()}", count: {len(answers)}}};</script>\n'
    (OUT / "index.html").write_text(page.replace("<script>\nconst $ =", flag + "<script>\nconst $ =", 1))
    print(len(answers), "answers;", round((OUT / "answers.json").stat().st_size / 1024), "KB")


if __name__ == "__main__":
    main()
