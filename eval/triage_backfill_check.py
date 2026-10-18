"""Numbers and a hand-check worksheet for the triage backfill (see eval/TRIAGE_BACKFILL.md).

  uv run python -m lineage.triage --backfill --since 2024-01-01     # the run itself
  uv run python -m eval.triage_backfill_check stats                  # action mix, drafts, routing, timing, edges
  uv run python -m eval.triage_backfill_check sheet [--seed 7]       # worksheet for the hand check, to stdout

Reads the outbox (data/triage/backfill/*.json) and the Triage nodes with mode='backfill'. Writes nothing.
The worksheet prints, for every decisive decision (or a seeded sample of 40 per the method), the topic's title
and opening text and each cited PEP / rejected idea / thread with its abstract or text, plus a seeded sample
of route_to_experts decisions for the recall check.
"""
import json
import random
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from lineage.db import graph

ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "data" / "triage" / "backfill"
RAW = ROOT / "data" / "raw" / "discourse.json"
DECISIVE = ("reply_with_ruling", "point_to_existing", "link_prior_threads")
ORDER = DECISIVE + ("flag_for_review", "route_to_experts")


def decisions() -> list[dict]:
    return sorted((json.loads(p.read_text()) for p in BOX.glob("*.json")),
                  key=lambda d: (d["topic"]["created"], d["topic"]["id"]))


def stats() -> None:
    ds = decisions()
    g = graph()
    acts = Counter(d["action"] for d in ds)
    print(f"decisions in outbox: {len(ds)}")
    for a in ORDER:
        print(f"  {a:<19} {acts[a]:>5}  {acts[a] / len(ds):.1%}")
    print(f"drafted reply: {sum(bool(d.get('draft')) for d in ds)}")
    print(f"routed to >=1 person: {sum(bool(d.get('reviewers')) for d in ds)}"
          f" (route_to_experts with nobody: {sum(d['action'] == 'route_to_experts' and not d['reviewers'] for d in ds)})")
    print(f"with opening text: {sum(bool(d['topic'].get('text')) for d in ds)}")
    # Wall time per decision: gaps between consecutive Triage.at stamps (second resolution) of the backfill run.
    ats = sorted(datetime.fromisoformat(r[0]) for r in
                 g.ro_query("MATCH (t:Triage) WHERE t.mode = 'backfill' RETURN t.at").result_set)
    gaps = [(b - a).total_seconds() for a, b in zip(ats, ats[1:]) if (b - a).total_seconds() < 120]
    if gaps:
        print(f"backfill Triage nodes: {len(ats)}; first {ats[0]}, last {ats[-1]}; "
              f"median gap {statistics.median(gaps):.1f}s, mean {statistics.mean(gaps):.2f}s, p90 "
              f"{sorted(gaps)[int(len(gaps) * 0.9)]:.0f}s")
    for rel in ("SAME_IDEA_AS", "RELATED_TO"):
        rows = g.ro_query(f"""MATCH (th:Thread)-[w:{rel}]->(x) WHERE w.by = 'triage'
                              MATCH (tr:Triage {{id: w.triage}}) WHERE tr.mode = 'backfill'
                              RETURN labels(x)[0], count(*)""").result_set
        print(f"{rel} edges from backfill: " + ", ".join(f"{lbl} {n}" for lbl, n in rows))
    by_year = Counter((d["topic"]["created"][:4], d["action"] in DECISIVE) for d in ds)
    for y in sorted({k[0] for k in by_year}):
        print(f"  {y}: {by_year[(y, True)]} decisive of {by_year[(y, True)] + by_year[(y, False)]}")


def describe(e: dict) -> str:
    g = graph()
    if e["kind"] == "thread":
        r = g.ro_query("MATCH (t:Thread {id: $i}) RETURN t.title, t.created, t.text", {"i": e["thread_id"]}).result_set
        t, c, x = r[0] if r else (e["title"], "", "")
        return f"THREAD {e['thread_id']} ({(c or '')[:10]}) {t}\n      {(x or '')[:500]}"
    r = g.ro_query("MATCH (p:PEP {number: $n}) RETURN p.title, p.status, p.created, p.abstract", {"n": e["pep"]}).result_set
    t, s, c, ab = r[0] if r else ("?", "?", "", "")
    out = f"PEP {e['pep']} [{s}, {(c or '')[:10]}] {t}"
    if e["kind"] == "rejected_idea":
        a = g.ro_query("MATCH (a:Alternative {pep: $n, title: $t}) RETURN a.text", {"n": e["pep"], "t": e["title"]}).result_set
        out += f"\n      REJECTED IDEA: {e['title']}: {((a[0][0] if a else '') or '')[:600]}"
    else:
        out += f"\n      {(ab or '')[:500]}"
    return out


def show(d: dict) -> None:
    t = d["topic"]
    print(f"\n=== {t['id']} {t['created'][:10]} {d['action']}  {t['url']}\n{t['title']}\n  TEXT: {(t.get('text') or '(title only)')[:700]}")
    print(f"  REASON: {d['reason']}")
    for e in d["evidence"]:
        print(f"  - {e['match']} {e.get('score')}: {describe(e)}")
    if d.get("draft"):
        print(f"  DRAFT: {d['draft'][:300]}")


def sheet(seed: int = 7) -> None:
    ds = decisions()
    rng = random.Random(seed)
    dec = [d for d in ds if d["action"] in DECISIVE]
    pick = dec if len(dec) <= 40 else sorted(rng.sample(dec, 40), key=lambda d: d["topic"]["id"])
    print(f"# decisive: {len(dec)}, shown: {len(pick)} (seed {seed})")
    for d in pick:
        show(d)
    routed = [d for d in ds if d["action"] == "route_to_experts"]
    print(f"\n\n# route_to_experts sample: 20 of {len(routed)} (seed {seed})")
    for d in sorted(random.Random(seed).sample(routed, min(20, len(routed))), key=lambda d: d["topic"]["id"]):
        show(d)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    seed = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 7
    stats() if cmd == "stats" else sheet(seed)
