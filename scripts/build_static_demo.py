"""Build docs/demo/: the real UI plus recorded answers, for a static live demo (GitHub Pages).

  uv run python scripts/build_static_demo.py
  uv run python scripts/build_static_demo.py --triage-only   # refresh triage.json and the page, keep the answers

Every answer in the snapshot is a real response from the running graph (tools, rows, paths, Cypher),
recorded by calling the agent exactly as the web server does. The crawl starts from the questions the
home page offers and follows each answer's suggested next questions, so every button on the demo
leads to a recorded answer. Anything else shows how to run Lineage locally to ask it.

It also writes the triage agent's recorded decisions (triage.json, up to 200, live and backfill), with the
evidence, reviewers, tool steps, knowledge edges and drafted reply each one recorded, for the triage queue.
"""
import json
import re
import sys
import uuid
from datetime import date
from pathlib import Path

from lineage import agent, triage
from lineage.db import graph

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "demo"
HERO = "Has anyone proposed null coalescing in Python?"   # must match HERO_Q in lineage/web/index.html
SEEDS = [
    HERO,
    "Should Python add a switch statement?",
    "Can I customise how a class is created without writing a metaclass?",
    "Draft a reply to someone proposing keyword arguments in indexing",
    "Why does Python have vectorcall?",
    "How are PEP 472 and PEP 696 related?",
    "Is PEP 563 still current?",
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


TRIAGE_BUDGET = 400 * 1024


def triage_json() -> tuple[str, int]:
    """The triage record for the queue, under TRIAGE_BUDGET: if it runs over, drop each step's args (the page
    doesn't show them), then shorten its why and outcome."""
    # Every live decision and every decisive backfill decision, then the most recent other backfill ones, so a long
    # backfill can't crowd the showcase cases out.
    rows, seen = [], set()
    batches = [triage.history(limit=500, mode="live")]
    batches += [triage.history(limit=500, mode="backfill", action=a)
                for a in ("reply_with_ruling", "point_to_existing", "link_prior_threads")]
    batches.append(triage.history(limit=120, mode="backfill"))
    for batch in batches:
        for r in batch:
            if (r.get("mode"), r["topic"]) not in seen:
                seen.add((r.get("mode"), r["topic"]))
                rows.append(r)
    text = json.dumps({"rows": rows}, default=str)
    if len(text.encode()) > TRIAGE_BUDGET:
        for r in rows:
            for st in r.get("steps") or []:
                st.pop("args", None)
        text = json.dumps({"rows": rows}, default=str)
    for cap in (240, 160, 100, 60):
        if len(text.encode()) <= TRIAGE_BUDGET:
            break
        for r in rows:
            for st in r.get("steps") or []:
                for k in ("why", "outcome"):
                    if isinstance(st.get(k), str) and len(st[k]) > cap:
                        st[k] = st[k][:cap - 1].rstrip() + "…"
        text = json.dumps({"rows": rows}, default=str)
    return text, len(rows)


def write_page(count: int, recorded: str | None = None) -> None:
    page = (ROOT / "lineage" / "web" / "index.html").read_text()
    flag = f'<script>window.LINEAGE_STATIC = {{date: "{recorded or date.today().isoformat()}", count: {count}}};</script>\n'
    (OUT / "index.html").write_text(page.replace("<script>\nconst $ =", flag + "<script>\nconst $ =", 1))


def triage_only() -> None:
    text, n = triage_json()
    (OUT / "triage.json").write_text(text)
    # The answers weren't re-recorded, so the page keeps the date they were.
    old = re.search(r'LINEAGE_STATIC = \{date: "([^"]+)"', (OUT / "index.html").read_text())
    write_page(len(json.loads((OUT / "answers.json").read_text())), old and old.group(1))
    print(n, "triage decisions;", round(len(text.encode()) / 1024), "KB")


def main() -> None:
    agent.ask("warm up", session="snapwarmup")   # load the models first, so recorded timings are real ones
    triage_text, n_decisions = triage_json()
    answers, queue, seen = {}, [(q, 0) for q in SEEDS], set()
    sessions = ["snapwarmup"]
    while queue:
        q, depth = queue.pop(0)
        if norm(q) in seen:
            continue
        seen.add(norm(q))
        sid = "snap" + uuid.uuid4().hex[:10]   # a fresh session per question: no memory links between them
        sessions.append(sid)
        res = agent.ask(q, session=sid)
        res.pop("session", None)
        res["question"] = q   # shown, in its own casing, where the snapshot lists what it recorded
        answers[norm(q)] = res
        if depth < 2:
            queue += [(f, depth + 1) for f in follow_ups(res)]
    graph().query("MATCH (s:Session) WHERE s.id IN $ids OPTIONAL MATCH (s)-[:ASKED]->(q) DETACH DELETE q, s",
                  {"ids": sessions})
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "answers.json").write_text(json.dumps(answers, default=str))
    (OUT / "triage.json").write_text(triage_text)
    # The home page's opening answer, for the live server and the snapshot alike.
    for where in (ROOT / "lineage" / "web", OUT):
        (where / "hero.json").write_text(json.dumps(answers[norm(HERO)], default=str))
    write_page(len(answers))
    print(len(answers), "answers;", round((OUT / "answers.json").stat().st_size / 1024), "KB;", n_decisions, "triage decisions")


if __name__ == "__main__":
    triage_only() if "--triage-only" in sys.argv[1:] else main()
