"""Lineage vs a vector-only baseline, on questions with known answers.

  uv run python -m eval.run        # writes eval/RESULTS.md and eval/results.json

Ground truth
  eval/questions.jsonl: hand-labelled, each item with a source link.
  Currency questions are generated here from data/raw/peps.json `superseded_by`, read directly
  rather than through the graph, plus non-superseded controls.

Baseline (given every advantage we could think of)
  The same corpus Lineage holds: PEP title + abstract, Motivation / Rationale / ruling-notice
  sections, and every rejected idea, embedded with the same model (bge-small). Top-5 chunks by
  cosine similarity. Currency questions also get the PEP's title in the query. For "already
  decided?" the no-match cutoff is tuned on this same question set (best case for the baseline).

Scoring
  No LLM key is used. The baseline is scored on evidence recall: do its 5 chunks contain what
  is needed to answer? That is an upper bound on any LLM answer built from them (ignoring the
  model's own memory). Lineage is scored on the rows its tool returns, strictly: for "already
  decided?" the top row must be a "same idea" match on an expected PEP, and for ideas with no
  PEP it must not claim a "same idea" match.
"""
import hashlib
import json
import time
from pathlib import Path

import numpy as np

from lineage import agent, tools
from lineage.db import graph

HERE = Path(__file__).parent
RAW = HERE.parent / "data" / "raw"
K = 5
CONTROLS = [8, 484, 572, 634, 3333]


# ---------------------------------------------------------------- questions

def currency_questions() -> list[dict]:
    peps = json.loads((RAW / "peps.json").read_text())

    def latest(n: int) -> int:
        seen = set()
        while (nxt := peps[str(n)].get("superseded_by")) and n not in seen:
            seen.add(n)
            n = int(str(nxt).split(",")[0])
        return n

    out = []
    for key, p in sorted(peps.items(), key=lambda kv: int(kv[0])):
        if p.get("superseded_by"):
            out.append({"id": f"cu{key}", "kind": "currency", "pep": int(key), "title": p["title"],
                        "q": f"Is PEP {key} ({p['title']}) still current, or was it superseded?",
                        "expect": latest(int(key))})
    for n in CONTROLS:
        out.append({"id": f"cu{n}", "kind": "currency", "pep": n, "title": peps[str(n)]["title"],
                    "q": f"Is PEP {n} ({peps[str(n)]['title']}) still current, or was it superseded?", "expect": None})
    return out


def questions() -> list[dict]:
    hand = [json.loads(line) for line in (HERE / "questions.jsonl").read_text().splitlines() if line.strip()]
    return hand + currency_questions()


# ---------------------------------------------------------------- vector-only baseline

class VectorBaseline:
    def __init__(self):
        g = graph()
        self.chunks = [{"pep": n, "text": f"PEP {n}: {t}. {a or ''}"} for n, t, a in g.ro_query(
            "MATCH (p:PEP) RETURN p.number, p.title, p.abstract").result_set]
        self.chunks += [{"pep": n, "text": f"{t}. {x}"} for n, t, x in g.ro_query(
            "MATCH (s:Section) RETURN s.pep, s.title, s.text").result_set]
        self.chunks += [{"pep": n, "text": f"{t}. {x}"} for n, t, x in g.ro_query(
            "MATCH (a:Alternative) RETURN a.pep, a.title, a.text").result_set]
        digest = hashlib.sha1("\n".join(c["text"] for c in self.chunks).encode()).hexdigest()[:12]
        cache = HERE / ".cache" / f"chunks-{digest}.npy"
        if cache.exists():
            self.vecs = np.load(cache)
        else:
            cache.parent.mkdir(exist_ok=True)
            self.vecs = np.array(list(tools._embedder().embed([c["text"] for c in self.chunks])))
            np.save(cache, self.vecs)
        self.vecs /= np.linalg.norm(self.vecs, axis=1, keepdims=True)

    def search(self, query: str, k: int = K) -> list[dict]:
        q = np.array(list(tools._embedder().query_embed([query]))[0])
        sims = self.vecs @ (q / np.linalg.norm(q))
        top = np.argsort(-sims)[:k]
        return [{**self.chunks[i], "sim": float(sims[i])} for i in top]


def mentions(chunk: dict, pep: int) -> bool:
    return f"PEP {pep}" in chunk["text"] and f"PEP {pep}0" not in chunk["text"]


def baseline_evidence(item: dict, hits: list[dict]) -> bool:
    k = item["kind"]
    if k == "already_decided":
        return any(h["pep"] in item["expect"] for h in hits)
    if k == "currency":
        return any(h["pep"] == item["expect"] or mentions(h, item["expect"]) for h in hits)
    if k == "decider":
        name = "Steering Council" if "Council" in item["expect"] else item["expect"]
        return any(name in h["text"] for h in hits)
    if k == "recurrence":
        return any(h["pep"] in item["expect"] and mentions(h, item["pep"]) for h in hits)
    if k == "connection":
        return any((h["pep"] == item["a"] and mentions(h, item["b"])) or (h["pep"] == item["b"] and mentions(h, item["a"]))
                   for h in hits)
    raise ValueError(k)


# ---------------------------------------------------------------- Lineage

def lineage_call(item: dict) -> dict:
    k = item["kind"]
    if k == "already_decided":
        return tools.call("already_decided", {"idea": item["idea"]})
    if k == "currency":
        return tools.call("is_current", {"pep": str(item["pep"])})
    if k in ("decider", "recurrence"):
        return tools.call("why", {"pep": str(item["pep"])})
    if k == "connection":
        return tools.call("connection", {"a": str(item["a"]), "b": str(item["b"])})
    raise ValueError(k)


def lineage_correct(item: dict, res: dict) -> bool:
    k, rows = item["kind"], res["rows"]
    if k == "already_decided":
        same = [r for r in rows if r["match"] == "same"]
        if not item["expect"]:
            return not same
        return bool(rows) and rows[0]["match"] == "same" and rows[0]["pep"] in item["expect"]
    if k == "currency":
        r = rows[0] if rows else {}
        if item["expect"] is None:
            return r.get("current") is True
        return r.get("current") is False and any(x["pep"] == item["expect"] for x in r.get("latest") or [])
    if k == "decider":
        d = (rows[0].get("decision") or {}) if rows else {}
        return any(item["expect"] in x["by"] for x in d.get("decided_by", []))
    if k == "recurrence":
        return bool(rows) and any(x["pep"] in item["expect"] for x in rows[0].get("set_aside_later_by", []))
    if k == "connection":
        return bool(res["paths"]) and any(x.get("edge") == item["expect"] for x in res["paths"][0])
    raise ValueError(k)


ROUTER_EXPECT = {"already_decided": {"already_decided"}, "currency": {"is_current"}, "decider": {"why"},
                 "recurrence": {"why", "already_decided"}, "connection": {"connection"}}


def routed_tool(question: str) -> str | None:
    steps = agent.route(question, "python")
    useful = [s["tool"] for s in steps if s["tool"] not in ("resolve", "recall")]
    return useful[-1] if useful else None


# ---------------------------------------------------------------- run

def main() -> None:
    items = questions()
    base = VectorBaseline()
    tools.call("already_decided", {"idea": "warm up the models"})
    results = []
    for it in items:
        t0 = time.perf_counter()
        query = it.get("idea") or it["q"]
        hits = base.search(query)
        b_ms = (time.perf_counter() - t0) * 1000
        res = lineage_call(it)
        tool = routed_tool(it["q"])
        results.append({
            "id": it["id"], "kind": it["kind"], "set": it.get("set"), "q": it["q"], "expect": it["expect"],
            "lineage": lineage_correct(it, res), "lineage_ms": res["ms"], "lineage_paths": len(res["paths"]),
            "lineage_outcome": res["outcome"],
            "lineage_top": [{k: r.get(k) for k in ("pep", "match", "score")} for r in res["rows"][:2]]
                           if it["kind"] == "already_decided" else None,
            "baseline_evidence": baseline_evidence(it, hits) if it.get("expect") not in (None, []) else None,
            "baseline_top1": hits[0]["pep"], "baseline_top1_sim": round(hits[0]["sim"], 3), "baseline_ms": round(b_ms, 1),
            "router_tool": tool, "router_ok": tool in ROUTER_EXPECT[it["kind"]]})

    # The baseline's best possible no-match cutoff for "already decided?": top-1 similarity below
    # the cutoff means "nothing recorded". Tuned on these same items.
    ad = [r for r in results if r["kind"] == "already_decided"]
    def base_ad_ok(r, cut):
        if not r["expect"]:
            return bool(r["baseline_top1_sim"] < cut)
        return bool(r["baseline_top1_sim"] >= cut and r["baseline_top1"] in r["expect"])
    cut = max((float(c) for c in np.arange(0.5, 1.0, 0.005)), key=lambda c: sum(base_ad_ok(r, c) for r in ad))
    for r in ad:
        r["baseline_answer_ok"] = base_ad_ok(r, cut)

    (HERE / "results.json").write_text(json.dumps({"cutoff": round(float(cut), 3), "results": results}, indent=1))
    write_report(results, float(cut))


def pct(xs) -> str:
    xs = [x for x in xs if x is not None]
    return f"{sum(xs)}/{len(xs)} ({100 * sum(xs) / len(xs):.0f}%)" if xs else "n/a"


def write_report(results: list[dict], cut: float) -> None:
    by = lambda k, sub=None: [r for r in results if r["kind"] == k and (sub is None or sub(r))]
    pos = lambda r: bool(r["expect"])
    neg = lambda r: not r["expect"]
    dev = lambda f: lambda r: r["set"] == "dev" and f(r)
    held = lambda f: lambda r: r["set"] == "held-out" and f(r)
    rows = [
        ("Already decided? has a PEP · **held-out**", by("already_decided", held(pos)), "top-1 chunk is an expected PEP (cutoff applied)", "baseline_answer_ok"),
        ("Already decided? no PEP · **held-out**", by("already_decided", held(neg)), "top-1 similarity under the tuned cutoff", "baseline_answer_ok"),
        ("Already decided? has a PEP · dev (seen while building)", by("already_decided", dev(pos)), "top-1 chunk is an expected PEP (cutoff applied)", "baseline_answer_ok"),
        ("Already decided? no PEP · dev", by("already_decided", dev(neg)), "top-1 similarity under the tuned cutoff", "baseline_answer_ok"),
        ("Already decided? has a PEP · all (evidence in top 5)", by("already_decided", pos), "an expected PEP anywhere in the top 5", "baseline_evidence"),
        ("Is it current? (superseded)", by("currency", lambda r: r["expect"] is not None), "top 5 include the latest PEP in the chain", "baseline_evidence"),
        ("Is it current? (controls)", by("currency", lambda r: r["expect"] is None), "(nothing to retrieve)", None),
        ("Who decided it?", by("decider"), "decider named in the top 5", "baseline_evidence"),
        ("Did the idea come back?", by("recurrence"), "a later PEP citing it in the top 5", "baseline_evidence"),
        ("How are A and B related?", by("connection"), "a chunk of A naming B (or B naming A) in the top 5", "baseline_evidence"),
    ]
    out = ["# Evaluation: Lineage vs vector-only retrieval", "",
           f"Generated by `uv run python -m eval.run` · {len(results)} questions · see the docstring of "
           "`eval/run.py` for exactly how each side is scored.", "",
           "| Question type | n | Lineage (tool rows) | Vector baseline | Baseline is credited when |", "|---|---|---|---|---|"]
    for name, rs, how, key in rows:
        out.append(f"| {name} | {len(rs)} | {pct([r['lineage'] for r in rs])} | "
                   f"{pct([r[key] for r in rs]) if key else 'n/a'} | {how} |")
    allr = results
    confident = [r for r in by("already_decided") if r["lineage_top"] and r["lineage_top"][0]["match"] == "same"]
    wrong = [r for r in confident if r["lineage_top"][0]["pep"] not in r["expect"]]
    out += ["", f"- **Confidently wrong** (Lineage's top row is a \"same idea\" match on a PEP that did not propose it): "
                f"{len(wrong)}/{len(confident)} confident answers ({', '.join(r['id'] for r in wrong) or 'none'}). "
                "`draft_reply` only quotes \"same idea\" matches, so this is the rate at which a draft would cite the wrong PEP.",
            f"- Baseline no-match cutoff (cosine similarity of the top chunk), tuned on these questions: **{cut:.3f}**.",
            f"- Lineage answers that carry a graph path: {pct([r['lineage_paths'] > 0 for r in allr if r['lineage'] and r['lineage_outcome'] == 'answered'])} "
            "of correct answered items (the baseline returns text chunks, never paths, by construction).",
            f"- Keyword router (no LLM) picked the right tool: {pct([r['router_ok'] for r in allr])}.",
            f"- Median latency: Lineage {np.median([r['lineage_ms'] for r in allr]):.0f} ms, "
            f"baseline {np.median([r['baseline_ms'] for r in allr]):.0f} ms (warm, CPU, local FalkorDB).", "",
            "## Misses", ""]
    for r in allr:
        if not r["lineage"]:
            out.append(f"- Lineage `{r['id']}` {r['q']} expected {r['expect']}, got {r['lineage_top'] or r['lineage_outcome']}")
    for r in allr:
        if r["kind"] == "already_decided" and not r["expect"] and not r.get("baseline_answer_ok"):
            out.append(f"- Baseline `{r['id']}` {r['q']} has no PEP, but its top chunk (PEP {r['baseline_top1']}, "
                       f"similarity {r['baseline_top1_sim']}) clears the cutoff, so it would cite that PEP")
    for r in allr:
        if not r["router_ok"]:
            out.append(f"- Router `{r['id']}` {r['q']} chose {r['router_tool']}")
    (HERE / "RESULTS.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()
