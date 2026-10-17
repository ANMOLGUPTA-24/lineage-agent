"""Triage agent: decides what to do with a new discuss.python.org Ideas topic, from what the graph shows,
and does it.

  uv run python -m lineage.triage --latest 10       # the 10 most recently created Ideas topics, live from the forum
  uv run python -m lineage.triage --topic 107510    # one topic
  uv run python -m lineage.triage --latest 10 --dry-run

For each topic the agent calls the same graph tools a person would (already_decided, who_knows, draft_reply),
picks one action, records the decision in the graph, and writes the action to an outbox for a human to send.
It never posts to the forum. Topics it has already triaged are skipped: the graph remembers.

  Actions, decided from the graph:
    reply_with_ruling     a PEP or rejected idea that is the same idea was turned down: draft the reply,
                          quoting the ruling, with every earlier and later attempt
    point_to_existing     the same idea was accepted: say where it lives
    link_prior_threads    the same idea is an open draft PEP, or (no PEP) was discussed on the forum before:
                          link that discussion
    flag_for_review       only related history: a human should decide, and here is what to read
    route_to_experts      nothing in the record: send it to the people this area builds on

  Written to the graph:
    (:Triage {id, topic, at, action, confidence, reason})-[:ABOUT]->(:Thread)
    (:Triage)-[:EVIDENCE]->(:PEP)
    (:Triage)-[:ROUTED_TO {why}]->(:Person)
"""
import argparse
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from lineage import tools
from lineage.db import graph

OUTBOX = Path(__file__).resolve().parents[1] / "data" / "triage"
ADOPTED = {"Final", "Accepted", "Active", "Provisional"}
RULED = {"Rejected", "Withdrawn", "Deferred", "Superseded"}   # a PEP that ended without being adopted
NEAR = 0.26   # cosine distance; past this the "nearest" earlier thread is about something else


def fetch_topic(tid: int) -> dict | None:
    """Title, opening post and date of one Ideas topic, from the forum's public JSON."""
    from lineage.ingest.discourse import get, text_of
    from lineage.ingest.rst import clip
    d = get(f"/t/{tid}.json")
    if not d:
        return None
    first = next((p for p in d.get("post_stream", {}).get("posts", []) if p.get("post_number") == 1), None)
    return {"id": tid, "title": d.get("title"), "created": d.get("created_at"),
            "url": f"https://discuss.python.org/t/{tid}",
            "text": clip(text_of(first["cooked"]), 1500) if first else ""}


def latest_topics(n: int) -> list[dict]:
    """The n most recently created Ideas topics (the default listing is by latest activity, which brings back
    old topics whenever someone replies to them)."""
    from lineage.ingest.discourse import get
    topics = (get("/c/ideas/6.json?order=created&page=0") or {}).get("topic_list", {}).get("topics", [])
    topics = sorted((t for t in topics if not t.get("pinned")), key=lambda t: t.get("created_at") or "", reverse=True)[:n]
    return [x for t in topics if (x := fetch_topic(t["id"]))]


def prior(rows: list[dict], topic: dict) -> list[dict]:
    """History from before the topic: never the topic's own thread, never anything newer than it."""
    when = (topic.get("created") or "9999")[:10]
    out = []
    for r in rows:
        created = (r.get("created") or "")[:10]
        if r["kind"] == "thread" and (r.get("thread_id") == topic.get("id") or not created or created >= when):
            continue
        # Dates are days: a PEP from the topic's own day is usually the topic's PEP, not history it ignored.
        if r["kind"] != "thread" and created and created >= when:
            continue
        if r["kind"] != "thread":   # a PEP's earlier tries and comebacks are dated too: drop the later ones
            r = {**r, **{k: [x for x in r[k] if not (x.get("created") or "")[:10] >= when]
                         for k in ("came_back", "earlier_tries", "earlier_forms") if isinstance(r.get(k), list)}}
        out.append(r)
    return out


def nearest_threads(topic: dict, k: int = 8) -> list[tuple[int, str, list[str]]]:
    """The forum threads closest to the topic by embedding (none of them a match), started before it,
    with the core developers who posted most in each: who to route to when the record has nothing."""
    when = (topic.get("created") or "9999")[:10]
    vec = list(tools._embedder().query_embed([topic["title"] + ". " + (topic.get("text") or "")[:240]]))[0].tolist()
    rows = graph().ro_query("""CALL db.idx.vector.queryNodes('Thread', 'embedding', $k, vecf32($v)) YIELD node, score
                               WHERE score <= $near AND node.id <> $t AND node.created IS NOT NULL
                                     AND substring(node.created, 0, 10) < $when
                               OPTIONAL MATCH (a:Person)-[e:POSTED_IN]->(node) WHERE a.forum_group = 'committers'
                               WITH node, score, a, e ORDER BY e.posts DESC
                               RETURN node.id, node.title, score, collect(a.name)
                               ORDER BY score""", {"k": k, "v": vec, "t": topic.get("id"), "when": when, "near": NEAR}).result_set
    return [(tid, title, [n for n in names if n]) for tid, title, _score, names in rows]


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def reviewers(rows: list[dict], topic: dict | None = None, limit: int = 3) -> list[dict]:
    """Who should see this: the matched PEP's authors, core developers who argued it on the forum, then
    the people the area builds on (PageRank over the area's influence graph), but only when the PEP is the
    same idea or its area is a named topic: a loosely related PEP's generic cluster says little about who
    knows this idea. With no history at all, the core developers most active in the nearest earlier
    threads (said to be the nearest, not a match)."""
    out: list[dict] = []
    def add(name, why):
        if name and not any(o["name"] == name for o in out) and len(out) < limit:
            out.append({"name": name, "why": why})
    pep = next((r for r in rows if r["kind"] != "thread" and isinstance(r.get("pep"), int)), None)
    for r in rows:
        if r["kind"] == "thread":
            for p in r.get("participants", []):
                if p.get("group") == "committers":
                    add(p["name"], f"core developer, posted in \"{r['title']}\"")
    if pep:
        for name in pep.get("authors", [])[:2]:
            add(name, f"author of PEP {pep['pep']}")
        wk = tools.call("who_knows", {"subject": str(pep["pep"])}) if len(out) < 2 else {"outcome": "skipped"}
        w = wk["rows"][0] if wk["outcome"] == "answered" else None
        if w and (pep["match"] == "same" or w["subject"].startswith("topic ")):
            for p in w.get("most_built_on") or w.get("people") or []:
                add(p["name"], f"ranked #{p.get('rank', '?')} in {w['subject']} by PageRank" if p.get("rank")
                    else f"works on {w['subject']}")
    if not rows and topic:
        for _tid, title, names in nearest_threads(topic):
            for name in names[:2]:
                add(name, f"core developer, posted in \"{title}\" (the nearest earlier thread, not a match)")
    return out


def decide(topic: dict) -> dict:
    """One decision from the graph, with the rows and paths behind it."""
    idea = (topic["title"] + ". " + (topic.get("text") or "")[:240]).strip()
    res = tools.call("already_decided", {"idea": idea})
    rows = prior(res.get("rows", []), topic) if res["outcome"] == "answered" else []
    same = [r for r in rows if r["match"] == "same"]
    decided = next((r for r in same if r["kind"] != "thread"), None)
    threads = [r for r in same if r["kind"] == "thread"]
    if decided and decided.get("status") in ADOPTED and decided["kind"] == "pep":
        action = "point_to_existing"
        reason = f"PEP {decided['pep']} ({decided['title']}) proposed the same idea and is {decided['status']}."
    elif decided and decided.get("status") == "Draft" and decided["kind"] == "pep":
        action = "link_prior_threads"   # an open draft is not a ruling: point at the discussion under way
        reason = (f"An open PEP ({decided['pep']}, {decided['title']}) already proposes this; join that discussion. "
                  "Nothing is decided yet.")
    elif decided and (decided["kind"] != "pep" or decided.get("status") in RULED):   # or an idea a PEP set aside
        action = "reply_with_ruling"
        where = f"PEP {decided['pep']}" if decided["kind"] == "pep" else f"a rejected idea in PEP {decided['pep']}"
        reason = f"{where} ({decided['title']}, {(decided.get('created') or '')[:4]}) covered the same idea; it was {decided['verdict']}."
        if decided.get("came_back"):
            reason += f" It came up again in {plural(len(decided['came_back']), 'later PEP')}."
    elif threads:
        action = "link_prior_threads"
        last = max(threads, key=lambda r: r.get("created") or "")
        reason = (f"No PEP, but the forum discussed the same idea {plural(len(threads), 'time')} before, "
                  f"most recently \"{last['title']}\" ({last['created'][:4]}).")
    elif rows:
        action = "flag_for_review"
        reason = f"Only related history ({plural(len(rows), 'item')}); a person should judge whether it is the same idea."
    else:
        action = "route_to_experts"
        reason = "Nothing in the graph matches closely. That is not proof the idea is new."
    who = reviewers(rows, topic)
    if action == "route_to_experts":
        reason += (" Routed to core developers from the nearest earlier forum threads." if who else
                   " No earlier thread is close either, so there is nobody to route to: a person should pick reviewers.")
    elif not who:
        reason += " Nobody in the evidence qualifies as a reviewer: a person should pick one."
    # The reply is drafted from the same rows the decision used: only history from before the topic.
    draft = tools.draft_from_rows(rows) if action in ("reply_with_ruling", "link_prior_threads", "point_to_existing") else None
    if draft and decided and decided.get("status") == "Draft":
        draft = (f"An open PEP already proposes this: PEP {decided['pep']} ({decided['title']}), {decided['url']}. "
                 "Nothing is decided yet; the place to take this is that PEP's discussion.\n\n" + draft)
    return {"topic": topic, "action": action, "reason": reason, "confidence": max((r["score"] for r in rows), default=None),
            "evidence": [{"kind": r["kind"], "pep": r.get("pep"), "thread_id": r.get("thread_id"), "title": r["title"],
                          "match": r["match"], "url": r.get("idea_url") or r.get("url")} for r in rows[:5]],
            "reviewers": who, "draft": draft,
            "paths": res.get("paths", [])[:12], "cypher": len(res.get("cypher", []))}


def history(topic: int | None = None, limit: int = 20) -> list[dict]:
    """Recorded decisions, newest first: what was decided, why, the evidence and who it went to."""
    rows = graph().ro_query("""MATCH (tr:Triage)-[:ABOUT]->(th:Thread) WHERE $t IS NULL OR tr.topic = $t
                               OPTIONAL MATCH (tr)-[:EVIDENCE]->(p:PEP)
                               OPTIONAL MATCH (tr)-[:ROUTED_TO]->(who:Person)
                               RETURN tr.topic, th.title, th.url, tr.at, tr.action, tr.reason,
                                      collect(DISTINCT p.number), collect(DISTINCT who.name)
                               ORDER BY tr.at DESC LIMIT $n""", {"t": topic, "n": limit}).result_set
    return [{"topic": r[0], "title": r[1], "url": r[2], "at": r[3], "action": r[4], "reason": r[5],
             "evidence_peps": r[6], "routed_to": r[7]} for r in rows]


def already_triaged(tid: int) -> bool:
    return bool(graph().ro_query("MATCH (t:Triage {topic: $t}) RETURN t LIMIT 1", {"t": tid}).result_set)


def record(d: dict) -> str:
    """Act: write the decision into the graph, then into the outbox (and a webhook, if one is configured)."""
    t, at = d["topic"], datetime.now(timezone.utc).isoformat(timespec="seconds")
    tid = f"triage-{t['id']}-{at}"
    g = graph()
    g.query("""MERGE (th:Thread {id: $t}) ON CREATE SET th.title = $title, th.url = $url, th.created = $created, th.kind = 'idea'
               CREATE (tr:Triage {id: $id, topic: $t, at: $at, action: $action, reason: $reason, confidence: $conf})
               CREATE (tr)-[:ABOUT {weight: 1.0}]->(th)""",
            {"t": t["id"], "title": t["title"], "url": t["url"], "created": t.get("created") or "", "id": tid, "at": at,
             "action": d["action"], "reason": d["reason"], "conf": d["confidence"]})
    peps = sorted({e["pep"] for e in d["evidence"] if isinstance(e.get("pep"), int)})
    if peps:
        g.query("""MATCH (tr:Triage {id: $id}) UNWIND $peps AS n MATCH (p:PEP {number: n})
                   MERGE (tr)-[:EVIDENCE {weight: 1.0}]->(p)""", {"id": tid, "peps": peps})
    if d["reviewers"]:
        g.query("""MATCH (tr:Triage {id: $id}) UNWIND $people AS r MATCH (p:Person {name: r.name})
                   MERGE (tr)-[e:ROUTED_TO]->(p) SET e.why = r.why, e.weight = 1.0""",
                {"id": tid, "people": d["reviewers"]})
    OUTBOX.mkdir(parents=True, exist_ok=True)
    (OUTBOX / f"{t['id']}.json").write_text(json.dumps({k: v for k, v in d.items() if k != "paths"}, indent=1, default=str))
    (OUTBOX / f"{t['id']}.md").write_text(outbox_text(d))
    if url := os.getenv("TRIAGE_WEBHOOK_URL"):   # e.g. a Slack or Discord incoming webhook; off by default
        body = json.dumps({"text": outbox_text(d)[:3500], "content": outbox_text(d)[:1900]}).encode()
        try:   # the graph and the outbox are the record; a failed notification must not stop the run
            urllib.request.urlopen(urllib.request.Request(url, body, {"Content-Type": "application/json"}), timeout=15)
        except (OSError, ValueError) as e:   # URLError and timeouts are OSErrors; a malformed URL is a ValueError
            print(f"webhook failed for {t['id']}: {e}")
    return tid


def outbox_text(d: dict) -> str:
    t = d["topic"]
    lines = [f"# {d['action'].replace('_', ' ')}: {t['title']}", "", t["url"], "", f"**Why:** {d['reason']}", ""]
    if d["reviewers"]:
        lines += ["**Send to:** " + "; ".join(f"{r['name']} ({r['why']})" for r in d["reviewers"]), ""]
    if d["evidence"]:
        lines += ["**Evidence from the graph:**"] + [f"- {e['match']}: {e['title']} {e['url'] or ''}" for e in d["evidence"]] + [""]
    if d["draft"]:
        lines += ["**Drafted reply (not posted):**", "", d["draft"]]
    return "\n".join(lines) + "\n"


def triage(topics: list[dict], dry_run: bool = False) -> list[dict]:
    out = []
    for t in topics:
        if not dry_run and already_triaged(t["id"]):
            print(f"skip   {t['id']}  {t['title'][:70]}  (already triaged)")
            continue
        d = decide(t)
        if not dry_run:
            d["triage_id"] = record(d)
        print(f"{d['action']:<19} {t['id']}  {t['title'][:70]}")
        out.append(d)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--latest", type=int, help="triage the N newest Ideas topics")
    ap.add_argument("--topic", type=int, action="append", help="triage one topic id (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="decide only; write nothing")
    a = ap.parse_args()
    topics = [x for tid in (a.topic or []) if (x := fetch_topic(tid))] + (latest_topics(a.latest) if a.latest else [])
    triage(topics, a.dry_run)


if __name__ == "__main__":
    main()
