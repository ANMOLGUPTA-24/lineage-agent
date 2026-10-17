"""What the triage agent does with the newest Ideas topics: the policy in dry-run mode (decide only, nothing
written to the graph or the outbox) over the 60 most recent Ideas topics in the cached forum data.

  uv run python -m eval.triage_eval      # writes eval/TRIAGE.md

Each topic is decided as if it had just been posted: prior() drops its own thread and anything newer.
This measures what the agent does (action mix, evidence, speed), not whether it is right; every decision
that cites evidence is listed with links so a person can check it.
"""
import json
import statistics
import time
from collections import Counter
from pathlib import Path

from lineage import triage

HERE = Path(__file__).parent
RAW = HERE.parent / "data" / "raw" / "discourse.json"
N = 60
ORDER = ["reply_with_ruling", "point_to_existing", "link_prior_threads", "flag_for_review", "route_to_experts"]


# Written by hand after reading the decisions; regenerating the table does not update it.
HAND_CHECK = """## Hand check (2026-10-17, 9 decisions, by reading the topic and evidence titles and links)

- reply_with_ruling, 1 of 1 checked: right. "Define a concurrency model (aka memory model)" is the idea of PEP 583,
  withdrawn in 2008.
- flag_for_review, 8 of 17 checked (the 8 newest, titles only): none should have been a ruling. One is arguably
  link_prior_threads (109407 "More of my ideas for Python 3.16", whose clear-screen idea was already in the
  earlier "My ideas for Python 3.16" thread); two have weak evidence (insertdict() vs a defaultlist thread,
  private-by-default imports vs PEP 690 lazy imports); the other five are fairly related history.
- route_to_experts was not hand-checked for missed matches. Spot checks during development found at least one
  (109406 "a helper for writing `__repr__`" has an earlier thread "New function `repr_args` in `pprint` or
  `reprlib`" that the cross-encoder scored -2.5, below the related cutoff of 1.0), so read 42 routings as
  "nothing confident found", not "new".
- The verdict quoted is a PEP's status today, not as of the topic's date; for topics from 2026 that is the same.
"""


def recent_topics(n: int = N) -> list[dict]:
    """Same selection as eval/recent_ideas.py: newest Ideas topics, opening text when the cache has it."""
    cache = json.loads(RAW.read_text())
    opening = {int(k): v.get("text", "") for k, v in cache["topics"].items()}
    recent = sorted((i for i in cache["ideas"].values() if i["id"] != 20), key=lambda i: i["created"], reverse=True)[:n]
    return [{"id": i["id"], "title": i["title"], "created": i["created"],
             "url": f"https://discuss.python.org/t/{i['id']}", "text": opening.get(i["id"], "")} for i in recent]


def evidence_md(e: dict) -> str:
    label = f"PEP {e['pep']}: {e['title']}" if e["kind"] != "thread" else f"thread: {e['title']}"
    return f"{e['match']}: [{label}]({e['url']})"


def main() -> None:
    topics = recent_topics()
    triage.decide(topics[0])   # load the embedding and reranking models outside the timing
    decisions, secs = [], []
    for t in topics:
        start = time.perf_counter()
        decisions.append(triage.decide(t))
        secs.append(time.perf_counter() - start)
        print(f"{decisions[-1]['action']:<19} {t['id']}  {t['title'][:70]}")
    counts = Counter(d["action"] for d in decisions)
    routed = sum(bool(d["reviewers"]) for d in decisions)
    out = ["# Triage agent on recent Ideas topics", "",
           f"The triage policy (`lineage/triage.py`, `decide()` only: dry run, nothing written) over the {N} most recent "
           f"Ideas topics in the cached forum data (newest {topics[0]['created'][:10]}, oldest {topics[-1]['created'][:10]}). "
           "Each topic is decided as if just posted: its own thread and anything newer are excluded.", "",
           "This measures **what the agent does**, not whether it is right. The evidence behind every decision that "
           "cites the record is listed below for a person to check. Method: `eval/triage_eval.py`.", "",
           "## Actions", "", "| Action | Topics |", "|---|---|",
           *[f"| {a} | {counts.get(a, 0)} |" for a in ORDER], "",
           f"Routed to at least one person: {routed} of {N}. "
           f"Median decision time {statistics.median(secs):.2f} s (max {max(secs):.2f} s), models warm, one CPU process.", ""]
    for action in ORDER[:3]:
        ds = [d for d in decisions if d["action"] == action]
        out += [f"## {action} ({len(ds)})", ""]
        if not ds:
            out += ["None.", ""]
            continue
        out += ["| Posted | Topic | Evidence (same idea first) | Routed to |", "|---|---|---|---|"]
        for d in ds:
            t = d["topic"]
            out.append(f"| {t['created'][:10]} | [{t['title']}]({t['url']}) | "
                       + "<br>".join(evidence_md(e) for e in d["evidence"][:3]) + " | "
                       + ", ".join(r["name"] for r in d["reviewers"]) + " |")
        out.append("")
    (HERE / "TRIAGE.md").write_text("\n".join(out) + "\n" + HAND_CHECK)
    print(dict(counts), f"median {statistics.median(secs):.2f}s")


if __name__ == "__main__":
    main()
