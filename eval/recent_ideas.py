"""Mini-study: how many of the newest discuss.python.org Ideas topics had already been decided or discussed?

  uv run python -m eval.recent_ideas      # writes eval/RECENT_IDEAS.md

For each of the N most recent Ideas topics, run already_decided on its title and keep only matches
the cross-encoder calls the same idea that came *before* the topic: a PEP or rejected idea created
earlier, or a different forum thread started earlier. The topic's own thread and anything newer are
excluded. This is automatic matching (3 of 32 confident matches were wrong in eval/RESULTS.md), so
every match is listed with its link for a human to check; the headline number is an estimate.
"""
import json
from pathlib import Path

from lineage import tools

HERE = Path(__file__).parent
RAW = HERE.parent / "data" / "raw" / "discourse.json"
N = 60


def main() -> None:
    cache = json.loads(RAW.read_text())
    ideas, opening = cache["ideas"].values(), {int(k): v.get("text", "") for k, v in cache["topics"].items()}
    recent = sorted((i for i in ideas if i["id"] != 20), key=lambda i: i["created"], reverse=True)[:N]
    rows, hits, related_hits = [], 0, 0
    for topic in recent:
        when = topic["created"][:10]
        # Title plus the start of the opening post when we have it (topics with 5+ posts).
        text = (topic["title"] + ". " + opening.get(topic["id"], "")[:240]).strip()
        res = tools.call("already_decided", {"idea": text})
        prior, related = [], False
        for r in res["rows"]:
            if r.get("match") not in ("same", "related"):
                continue
            created = (r.get("created") or "")[:10]
            if r["kind"] == "thread" and (r["thread_id"] == topic["id"] or not created or created >= when):
                continue
            if r["kind"] != "thread" and created and created >= when:
                continue
            label = (f"thread: {r['title']} ({created[:4]})" if r["kind"] == "thread"
                     else f"PEP {r['pep']}: {r.get('idea') or r['title']} ({created[:4]}, {r['status']})")
            url = r.get("idea_url") or r.get("url")
            if r["match"] == "same":
                prior.append(f"**same:** [{label}]({url})")
            else:
                related = True
                prior.append(f"related: [{label}]({url})")
        hits += any(x.startswith("**same") for x in prior)
        related_hits += bool(prior)
        rows.append(f"| {when} | [{topic['title']}](https://discuss.python.org/t/{topic['id']}) | {topic['posts']} | "
                    + ("<br>".join(prior[:2]) if prior else "–") + " |")
    out = [f"# Recent Ideas topics vs the record", "",
           f"The {N} most recent Ideas topics in the cached data (newest {recent[0]['created'][:10]}, oldest {recent[-1]['created'][:10]}).",
           f"**{hits} of {N} ({100 * hits // N}%)** had an earlier PEP, rejected idea or forum thread that Lineage judged to be **the same idea**; "
           f"**{related_hits} of {N} ({100 * related_hits // N}%)** had earlier **same or related** history.",
           "", "Read this as a lower bound with a caveat: many recent Ideas topics are narrow and genuinely new (C API macros, single-module",
           "tweaks), only threads with 5+ posts carry their opening text, and the same-idea cutoff is strict by design (a draft reply should",
           "only cite a true match).",
           "", "Automatic matching: check the links. Method: `eval/recent_ideas.py` docstring.", "",
           "| Posted | Topic | Posts | Earlier record Lineage found |", "|---|---|---|---|", *rows]
    (HERE / "RECENT_IDEAS.md").write_text("\n".join(out) + "\n")
    print(f"{hits}/{N}")


if __name__ == "__main__":
    main()
