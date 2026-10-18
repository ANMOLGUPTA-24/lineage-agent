"""Triage agent: decides what to do with a new discuss.python.org Ideas topic, from what the graph shows,
and does it.

  uv run python -m lineage.triage --latest 10       # the 10 most recently created Ideas topics, live from the forum
  uv run python -m lineage.triage --topic 107510    # one topic (add --redo to forget an earlier decision first)
  uv run python -m lineage.triage --latest 10 --dry-run
  uv run python -m lineage.triage --backfill --since 2024-01-01 [--limit N] [--dry-run]
                                                    # cached Ideas topics (data/raw/discourse.json), oldest first;
                                                    # skips topics already triaged, so a stopped run resumes

For each topic the agent plans as it goes: it asks already_decided first, then picks the next graph tool from
what the last one showed (is_current when a matched PEP was superseded or is the one to point at, why when its
ruling is missing, who_knows / connection to pick reviewers for related history, the nearest earlier threads when
nothing matches), and drafts a reply only when the action sends one. Every step is kept with why it was chosen
and what it found. It never posts to the forum. Topics it has already triaged are skipped: the graph remembers.

  Actions, decided from the graph:
    reply_with_ruling     a PEP or rejected idea that is the same idea was turned down: draft the reply,
                          quoting the ruling, with every earlier and later attempt
    point_to_existing     the same idea was accepted (or a PEP that is the same idea was superseded by one that
                          was): say where it lives now
    link_prior_threads    the same idea is an open draft PEP, or (no PEP) was discussed on the forum before:
                          link that discussion
    flag_for_review       only related history: a human should decide, and here is what to read
    route_to_experts      nothing in the record: send it to the people this area builds on

  Written to the graph:
    (:Triage {id, topic, at, mode, action, confidence, reason, draft, steps})-[:ABOUT]->(:Thread)
    (:Triage)-[:EVIDENCE]->(:PEP), (:Triage)-[:EVIDENCE]->(:Thread)   the PEPs and earlier forum threads it relied on
    (:Triage)-[:ROUTED_TO {why}]->(:Person)
    (:Thread)-[:SAME_IDEA_AS|RELATED_TO {by: 'triage', triage, confidence, at}]->(:PEP | :Thread)
        knowledge write-back: the topic's thread is linked to what it repeats, so the next question about it
        (or a later topic that matches it) starts from that link
"""
import argparse
import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from redis.exceptions import ResponseError

from lineage import tools
from lineage.db import graph

ROOT = Path(__file__).resolve().parents[1]
OUTBOX = ROOT / "data" / "triage"
RAW = ROOT / "data" / "raw" / "discourse.json"
ADOPTED = {"Final", "Accepted", "Active", "Provisional"}
RULED = {"Rejected", "Withdrawn", "Deferred", "Superseded"}   # a PEP that ended without being adopted
NEAR = 0.26   # cosine distance; past this the "nearest" earlier thread is about something else
ACTIONS = ("reply_with_ruling", "point_to_existing", "link_prior_threads", "flag_for_review", "route_to_experts")
MODES = ("live", "backfill")
WRITES = {"same": "SAME_IDEA_AS", "related": "RELATED_TO"}
ROLE = {"authored": "author of", "delegate_for": "decided"}   # who_knows' "direct" roles, as a reviewer's why
# point_to_existing says "this already exists", so it needs more than a same-idea match (tools.SAME_IDEA = 5.0).
# The bands were set on hand-checked ideas where true matches scored 6.0-7.9; in the 2024-26 backfill
# (eval/TRIAGE_BACKFILL.md) none of the 18 point_to_existing decisions was fully right, and the 4 that scored 5.0-6.0
# without naming the PEP were promotions or extensions (55503 at 4.97 via a thread, 101401 at 5.81, 106430 at 5.76).
# Known-good cases score 6.6 (PEP 572, PEP 814) to 8.9 (PEP 765). Below POINT_MIN it is flagged for a person.
POINT_MIN = 6.0
VERDICT_THEN = "still open when the topic was posted"
WRITES["builds_on"] = "RELATED_TO"   # a post that builds on a PEP is related to it, not the same idea


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


def cached_topics(since: str, path: Path = RAW) -> list[dict]:
    """Ideas topics from the forum cache created on or after `since`, oldest first: title plus the cached
    opening text (kept for topics with 5+ posts), as in eval/recent_ideas.py. No network."""
    cache = json.loads(path.read_text())
    opening = {int(k): v.get("text") or "" for k, v in cache.get("topics", {}).items()}
    ideas = sorted((i for i in cache["ideas"].values() if i["id"] != 20 and (i.get("created") or "") >= since),
                   key=lambda i: (i["created"], i["id"]))
    return [{"id": i["id"], "title": i["title"], "created": i["created"], "url": f"https://discuss.python.org/t/{i['id']}",
             "text": opening.get(i["id"], "")[:1500]} for i in ideas]


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


def nearest_threads(topic: dict, k: int = 8) -> list[dict]:
    """The forum threads closest to the topic by embedding (none of them a match), started before it, with
    the core developers who posted most in each and the PEPs each discusses: who to route to when the record
    has nothing."""
    when = (topic.get("created") or "9999")[:10]
    vec = list(tools._embedder().query_embed([topic["title"] + ". " + (topic.get("text") or "")[:240]]))[0].tolist()
    rows = graph().ro_query("""CALL db.idx.vector.queryNodes('Thread', 'embedding', $k, vecf32($v)) YIELD node, score
                               WHERE score <= $near AND node.id <> $t AND node.created IS NOT NULL
                                     AND substring(node.created, 0, 10) < $when
                               OPTIONAL MATCH (a:Person)-[e:POSTED_IN]->(node) WHERE a.forum_group = 'committers'
                               WITH node, score, a, e ORDER BY e.posts DESC
                               WITH node, score, collect(a.name) AS names
                               OPTIONAL MATCH (node)-[:DISCUSSES|MENTIONS]->(p:PEP)
                               RETURN node.id, node.title, score, names, collect(DISTINCT p.number)
                               ORDER BY score""", {"k": k, "v": vec, "t": topic.get("id"), "when": when, "near": NEAR}).result_set
    return [{"id": tid, "title": title, "committers": [n for n in names if n], "peps": [n for n in nums if n]}
            for tid, title, _score, names, nums in rows]


def earlier_triage(thread_ids: list[int]) -> list[dict]:
    """PEPs that earlier triage runs linked these forum threads to as the same idea (the write-back)."""
    if not thread_ids:
        return []
    rows = graph().ro_query("""MATCH (th:Thread)-[w:SAME_IDEA_AS]->(p:PEP) WHERE th.id IN $ids AND w.by = 'triage'
                               RETURN th.id, th.title, p.number, p.title, p.status, p.created, w.at ORDER BY w.at""",
                            {"ids": thread_ids}).result_set
    return [{"thread_id": t, "thread": tt, "pep": n, "title": pt, "status": s, "created": c, "at": at}
            for t, tt, n, pt, s, c, at in rows]


def pep_brief(n: int) -> dict:
    rows = graph().ro_query("MATCH (p:PEP {number: $n}) RETURN p.title, p.status, p.url, p.created", {"n": n}).result_set
    title, status, url, created = rows[0] if rows else (None, None, f"https://peps.python.org/pep-{n:04d}/", None)
    return {"pep": n, "title": title, "status": status, "url": url, "created": created}


def plural(n: int, word: str) -> str:
    if word == "person":
        return f"{n} {'person' if n == 1 else 'people'}"
    return f"{n} {word}" + ("" if n == 1 else "es" if word.endswith(("ch", "sh", "s", "x")) else "s")


def label(r: dict) -> str:
    if r["kind"] == "thread":
        return f"thread \"{r['title']}\" ({(r.get('created') or '')[:4]})"
    if r["kind"] == "rejected_idea":
        return f"rejected idea \"{r.get('idea')}\" in PEP {r['pep']} ({r.get('status')})"
    return f"PEP {r['pep']} ({r.get('status')})"


class Trace:
    """The agent's plan as it ran: each graph tool it chose, why (what in the graph made it choose it), and
    what came back. Paths and query counts are kept for the explanation."""

    def __init__(self):
        self.steps: list[dict] = []
        self.paths: list = []
        self.cypher = 0

    def call(self, tool: str, args: dict, why: str) -> dict:
        try:
            res = tools.call(tool, args)
        except ResponseError as e:   # a slow query (algo.SPpaths around a hub) must not lose the whole decision
            if "timed out" not in str(e).lower() or tool == "already_decided":
                raise
            res = {"outcome": "timed_out", "rows": [], "paths": [], "note": str(e)}
        self.paths += res.get("paths") or []
        self.cypher += len(res.get("cypher") or [])
        self.steps.append({"tool": tool, "args": args, "why": why, "outcome": res.get("outcome", "")})
        return res

    def local(self, tool: str, args: dict, why: str) -> dict:
        """A step that is a graph read inside the agent, not a catalogue tool (nearest threads, earlier triage)."""
        self.steps.append({"tool": tool, "args": args, "why": why, "outcome": ""})
        return self.steps[-1]

    def found(self, outcome: str) -> None:
        self.steps[-1]["outcome"] = outcome


def reviewers(rows: list[dict], topic: dict | None = None, limit: int = 3, trace: Trace | None = None) -> list[dict]:
    """Who should see this: core developers who argued it on the forum, the matched PEP's authors, then the
    people the area builds on (PageRank over the area's influence graph), but only when the PEP is the same
    idea or its area is a named topic: a loosely related PEP's generic cluster says little about who knows
    this idea. For related history, connection() checks how close each forum reviewer is to the related PEP.
    With no history at all, the core developers most active in the nearest earlier threads (said to be the
    nearest, not a match), then who knows the PEP those threads discuss."""
    trace = trace or Trace()
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
    from_threads = list(out)
    linked = [(r, x["pep"]) for r in rows if r["kind"] == "thread" for x in r.get("peps", [])]
    if not pep and linked and not from_threads:
        r, n = linked[0]
        res = trace.call("who_knows", {"subject": str(n)},
                         f"no core developer posted in the matching forum threads, but \"{r['title']}\" "
                         f"{'is the discussion of' if r['peps'][0].get('how') == 'DISCUSSES' else 'refers to'} PEP {n}: "
                         "ask who knows that PEP")
        w = res["rows"][0] if res["outcome"] == "answered" else None
        for p in (w.get("direct") or []) if w else []:
            add(p["name"], f"{ROLE.get(p.get('role'), 'works on')} PEP {n}, which \"{r['title']}\" discusses")
        trace.found(f"added {plural(len(out), 'person')}" if w else res["outcome"])
    if pep and pep["match"] == "related" and from_threads:
        # Related history only: is the forum reviewer close to the related decided record, or just nearby?
        hops = {}
        for o in from_threads[:2]:
            res = trace.call("connection", {"a": o["name"], "b": f"PEP {pep['pep']}"},
                             f"{o['name']} posted in a related forum thread and PEP {pep['pep']} is the closest decided "
                             "record: check how close they are to it in the graph before routing to them")
            row = (res.get("rows") or [{}])[0]
            hops[o["name"]] = row.get("hops") if res["outcome"] == "answered" else None
            trace.found(f"{row['hops']} hop(s), {row.get('strength')}" if hops[o["name"]] is not None
                        else "timed out: kept, unranked" if res["outcome"] == "timed_out" else "no path within 6 hops")
            if hops[o["name"]] is not None and hops[o["name"]] <= 2:
                o["why"] += f"; {plural(hops[o['name']], 'hop')} from PEP {pep['pep']} in the graph"
        out.sort(key=lambda o: hops.get(o["name"]) if hops.get(o["name"]) is not None else 99)
    if pep:
        for name in pep.get("authors", [])[:2]:
            add(name, f"author of PEP {pep['pep']}")
        if len(out) < 2:
            res = trace.call("who_knows", {"subject": str(pep["pep"])},
                             f"only {plural(len(out), 'reviewer')} from the evidence; ask who the area of PEP {pep['pep']} "
                             f"({pep['match']} idea) builds on")
            w = res["rows"][0] if res["outcome"] == "answered" else None
            usable = bool(w) and (pep["match"] == "same" or w["subject"].startswith("topic "))
            before = len(out)
            if usable:
                for p in w.get("most_built_on") or w.get("people") or []:
                    add(p["name"], f"ranked #{p.get('rank', '?')} in {w['subject']} by PageRank" if p.get("rank")
                        else f"works on {w['subject']}")
            trace.found((f"{w['subject']}: added {plural(len(out) - before, 'person')}" if len(out) > before
                         else f"{w['subject']}: nobody beyond the reviewers already listed") if usable
                        else f"{w['subject']} is a generic cluster for a related PEP: not used" if w
                        else res["outcome"])
    if not rows and topic:
        step = trace.local("nearest_threads", {"topic": topic.get("id")},
                           "already_decided found nothing from before the topic: the nearest earlier forum threads by "
                           "embedding show who works nearby (not a match)")
        near = nearest_threads(topic)
        for t in near:
            for name in t["committers"][:2]:
                add(name, f"core developer, posted in \"{t['title']}\" (the nearest earlier thread, not a match)")
        step["outcome"] = (f"{plural(len(near), 'thread')} within distance {NEAR}; {plural(len(out), 'core developer')}"
                           if near else f"no earlier thread within distance {NEAR}")
        linked = [(t, n) for t in near for n in t["peps"]]
        if len(out) < 2 and linked:
            t, n = linked[0]
            res = trace.call("who_knows", {"subject": str(n)},
                             f"only {plural(len(out), 'core developer')} in the nearest threads, and \"{t['title']}\" "
                             f"discusses PEP {n}: ask who knows that PEP")
            w = res["rows"][0] if res["outcome"] == "answered" else None
            before = len(out)
            if w:   # a PEP near the idea, not a match: its own authors and decider, not its whole cluster
                people = w.get("direct") or ([] if "most_built_on" in w else w.get("people") or [])
                for p in people:
                    add(p["name"], f"{ROLE.get(p.get('role'), 'works on')} {w['subject']}, which \"{t['title']}\" "
                                   "discusses (the nearest earlier thread, not a match)")
            trace.found(f"added {plural(len(out) - before, 'person')}" if w else res["outcome"])
    return out


def summarize(res: dict, rows: list[dict]) -> str:
    if res["outcome"] != "answered":
        return "nothing in the graph matches closely"
    dropped = len(res.get("rows", [])) - len(rows)
    return (f"{plural(len(res.get('rows', [])), 'match')}"
            + (f", {dropped} from on or after the topic's date (dropped)" if dropped else "")
            + (": " + "; ".join(f"{r['match']}: {label(r)}" for r in rows) if rows else ""))


def decide(topic: dict, mode: str = "live") -> dict:
    """One decision from the graph, with the steps, rows and paths behind it."""
    tr = Trace()
    idea = (topic["title"] + ". " + (topic.get("text") or "")[:240]).strip()
    res = tr.call("already_decided", {"idea": idea},
                  "every topic starts from the record: does a PEP, a rejected idea or a forum thread already cover "
                  "the topic's title and opening post?")
    rows = prior(res.get("rows", []), topic) if res["outcome"] == "answered" else []
    tr.found(summarize(res, rows))
    # already_decided promotes a PEP to "same" when a same-idea thread refers to it. If that thread is the topic's
    # own (or a later one), what the graph shows is that the post cites the PEP: for a ruled PEP that is a
    # re-proposal (PEP 505 in t/107510), for an adopted one the post builds on it (see cites()).
    kept = {r["thread_id"] for r in rows if r["kind"] == "thread"}
    for r in rows:
        if r["kind"] != "thread" and r["match"] == "same" and r.get("via_thread") and r["via_thread"] not in kept:
            r["cited_by_topic"] = True
            tr.steps[-1]["outcome"] += (f"; PEP {r['pep']} is a same-idea match only because the topic's own or a later "
                                        f"thread (t/{r['via_thread']}) refers to it: the post cites it")
    same = [r for r in rows if r["match"] == "same"]
    decided = next((r for r in same if r["kind"] != "thread"), None)
    # An idea a PEP considered and set aside is a closer answer than the PEP that was adopted without it.
    alt = next((r for r in same if r["kind"] == "rejected_idea"), None)
    if alt and decided is not alt and decided.get("status") in ADOPTED:
        decided = alt
    threads = [r for r in same if r["kind"] == "thread"]
    extra, latest, then = [], None, None   # `then`: the PEP as it stood when the topic was posted, for the draft
    when = (topic.get("created") or "9999")[:10]
    action = reason = found = None

    if decided:
        n, where = decided["pep"], (f"PEP {decided['pep']}" if decided["kind"] == "pep"
                                    else f"a rejected idea in PEP {decided['pep']}")
        status = decided.get("status")
        if decided.get("superseded_by") or status == "Superseded":
            latest = follow_chain(tr, n, f"already_decided reports PEP {n} was superseded by {decided['superseded_by']}"
                                  if decided.get("superseded_by") else f"PEP {n} is Superseded", when)
        target = latest if latest and latest["status"] in ADOPTED else (
            {"pep": n, "title": decided["title"], "status": status} if decided["kind"] == "pep" else None)
        # Status at the time of the topic: a PEP decided after the topic was posted was still open then.
        decided_on = decision_at(target["pep"]) if target else None
        if target and (target["status"] in ADOPTED | RULED) and decided_on and decided_on[:10] >= when:
            tr.local("status_then", {"pep": target["pep"]},
                     f"PEP {target['pep']} is {target['status']} today, but the topic is from {when}: check when it was "
                     "decided before quoting today's status")["outcome"] = f"decided {decided_on[:10]}, after the topic: open then"
            action = "link_prior_threads"
            reason = (f"An open PEP ({target['pep']}, {target['title']}) already proposed this when the topic was posted; "
                      f"join that discussion. Nothing was decided yet: it was {target['status']} only on {decided_on[:10]}.")
            then = {"pep": target["pep"], "status": "Draft", "verdict": VERDICT_THEN, "decision": None, "reason": None}
        elif target and target["status"] in ADOPTED:
            found = scan_rejected_ideas(tr, target, topic)
            score = decided.get("score") or 0
            if found and found["score"] >= tools.SAME_IDEA:
                action, decided = "reply_with_ruling", found
                rows.insert(0, found)
                for r in rows:
                    if r is not found and r["kind"] == "pep" and r["pep"] == found["pep"]:
                        r["match"] = "related"   # the PEP was adopted, but without this idea
                reason = (f"PEP {found['pep']} ({found['title']}, {(found.get('created') or '')[:4]}) considered "
                          f"\"{found['idea']}\" and set it aside, though the PEP itself was {target['status']}: "
                          "that is the ruling on this idea.")
            elif cites(topic, target) or (target["pep"] == n and decided.get("cited_by_topic")):
                action = "flag_for_review"
                reason = (f"The post builds on PEP {target['pep']} ({target['title']}, {target['status']}): it names the PEP, "
                          "so it is extending or revising it, not repeating it. Its authors should see it.")
                builds_on(rows, target["pep"])
            elif not latest and score < POINT_MIN:
                action = "flag_for_review"
                reason = (f"PEP {n} ({decided['title']}, {status}) matched at {score}, below the {POINT_MIN} needed to say "
                          "the idea already exists; a person should judge whether it is the same idea.")
                decided["match"] = "related"
            else:
                if not latest:   # adopted and confidently the same idea: is it still the record to point at?
                    latest = follow_chain(tr, n, f"PEP {n} is {status} and matched at {score}", when)
                    target = latest if latest and latest["status"] in ADOPTED else target
                action = "point_to_existing"
                reason = (f"{where} ({decided['title']}, {(decided.get('created') or '')[:4]}) proposed the same idea and is "
                          f"{status}, but it was superseded by PEP {latest['pep']} ({latest['title']}, {latest['status']}): "
                          "that is the record that describes Python now." if latest and latest["status"] in ADOPTED
                          else f"PEP {n} ({decided['title']}) proposed the same idea and is {status}.")
        elif status == "Draft" and decided["kind"] == "pep":
            action = "link_prior_threads"   # an open draft is not a ruling: point at the discussion under way
            reason = (f"An open PEP ({n}, {decided['title']}) already proposes this; join that discussion. "
                      "Nothing is decided yet.")
        else:   # a ruled PEP, or an idea a PEP set aside
            action = "reply_with_ruling"
            reason = (f"{where} ({decided['title']}, {(decided.get('created') or '')[:4]}) covered the same idea; "
                      f"it was {decided['verdict']}.")
            if latest:
                reason += f" It was later superseded by PEP {latest['pep']} ({latest['title']}, {latest['status']})."
        if action in ("reply_with_ruling", "point_to_existing", "link_prior_threads") and not then:
            back = [b for b in decided.get("came_back") or [] if not latest or b["pep"] != latest["pep"]]
            if back:
                reason += (f" It came up again in {plural(len(back), 'later PEP')}: "
                           + ", ".join(f"PEP {b['pep']} ({(b.get('created') or '')[:4]}, {b.get('status')})" for b in back[:3]) + ".")
        if action in ("reply_with_ruling", "point_to_existing") and not decided.get("decision") and not latest \
                and decided["kind"] == "pep":
            w = tr.call("why", {"pep": str(n)},
                        f"the reply quotes PEP {n}'s ruling, but the graph traversal from it found no Decision or decider: "
                        "look for its ruling notice and lineage before quoting it")
            row = (w.get("rows") or [{}])[0]
            if row.get("decision"):
                tr.found(f"ruling found: {row['decision']['outcome']}")
            elif row.get("ruling_notice") or decided.get("reason"):
                tr.found("no recorded decider; the PEP's own ruling notice is quoted")
                reason += f" The graph records no decider for PEP {n}; the reply quotes the PEP's own notice."
            else:
                tr.found(f"no ruling recorded, only its status ({status})")
                reason += f" The graph records no ruling for PEP {n}, only its status ({status}); the reply says so and where the discussion stopped."
        if action == "flag_for_review" and not any(r["match"] == "same" and r["kind"] != "thread" for r in rows):
            decided = None   # nothing decisive left: reviewers and the draft treat it as related history
    elif threads:
        action = "link_prior_threads"
        last = max(threads, key=lambda r: r.get("created") or "")
        reason = (f"No PEP, but the forum discussed the same idea {plural(len(threads), 'time')} before, "
                  f"most recently \"{last['title']}\" ({last['created'][:4]}).")
        step = tr.local("earlier_triage", {"threads": [r["thread_id"] for r in threads]},
                        "no PEP matched, but the matching threads may have been triaged before: read the SAME_IDEA_AS "
                        "links earlier runs wrote from them")
        # Only PEPs from before the topic: the same prior-history rule as the rows.
        links = [x for x in earlier_triage([r["thread_id"] for r in threads]) if (x["created"] or "9999")[:10] < when]
        step["outcome"] = ("; ".join(f"thread {x['thread_id']} -> PEP {x['pep']} ({x['status']})" for x in links)
                           if links else "none")
        if links:
            x = links[-1]
            reason += (f" An earlier triage linked \"{x['thread']}\" to PEP {x['pep']} ({x['title']}, {x['status']}) as "
                       "the same idea; read that PEP too.")
            extra.append({"kind": "pep", "pep": x["pep"], "thread_id": None, "title": x["title"], "match": "via_triage",
                          "url": pep_brief(x["pep"])["url"], "score": None})
    elif rows:
        action = "flag_for_review"
        reason = f"Only related history ({plural(len(rows), 'item')}); a person should judge whether it is the same idea."
    else:
        action = "route_to_experts"
        reason = "Nothing in the graph matches closely. That is not proof the idea is new."

    who = reviewers(rows, topic, limit=6, trace=tr)   # more than needed: inactive people are dropped below
    built = next((r["pep"] for r in rows if r["match"] == "builds_on"), None)
    if built:   # the post builds on this PEP: its authors come first
        authors = [{"name": a, "why": f"author of PEP {built}, which the post builds on"} for a in pep_authors(built)[:2]]
        who = authors + [w for w in who if w["name"] not in {a["name"] for a in authors}]
    for x in extra:
        if x["match"] == "via_triage" and len(who) < 3:
            for name in pep_authors(x["pep"])[:1]:
                if not any(o["name"] == name for o in who):
                    who.append({"name": name, "why": f"author of PEP {x['pep']}, which an earlier triage linked to this idea"})
    who, dropped = active_only(tr, who, when)
    if dropped and not who:
        reason += " Nobody active found; a moderator should pick reviewers."
    elif action == "route_to_experts":
        reason += (" Routed to core developers from the nearest earlier forum threads." if who else
                   " No earlier thread is close either, so there is nobody to route to: a person should pick reviewers.")
    elif not who:
        reason += " Nobody in the evidence qualifies as a reviewer: a person should pick one."

    # The reply is drafted from the same rows the decision used: only history from before the topic.
    # A PEP decided after the topic is drafted as it stood then: open, no ruling, no ruling notice.
    draft, same = None, [r for r in rows if r["match"] == "same"]
    if action in ("reply_with_ruling", "link_prior_threads", "point_to_existing"):
        tr.local("draft_reply", {}, f"{action} sends a reply: draft it from the {plural(len(same), 'same-idea row')} the "
                                    "decision used, nothing newer than the topic"
                                    + (f", with PEP {then['pep']} as it stood then (open)" if then else ""))
        as_then = [({**r, **then} if then and r["kind"] == "pep" and r.get("pep") == then["pep"] else r) for r in rows]
        draft = tools.draft_from_rows(as_then)
        opened = then or (decided if decided and decided["kind"] == "pep" and decided.get("status") == "Draft" else None)
        if draft and opened:
            o = next((r for r in as_then if r["kind"] == "pep" and r.get("pep") == opened["pep"]), decided)
            draft = (f"An open PEP already proposes this: PEP {o['pep']} ({o['title']}), {o['url']}. "
                     "Nothing is decided yet; the place to take this is that PEP's discussion.\n\n" + draft)
        tr.found(f"drafted ({len(draft)} characters, not posted)" if draft else "nothing to draft")
    evidence = [{"kind": r["kind"], "pep": r.get("pep"), "thread_id": r.get("thread_id"), "title": r["title"],
                 "match": r["match"], "url": r.get("idea_url") or r.get("url"), "score": r.get("score")} for r in rows[:5]]
    if latest and not any(e["pep"] == latest["pep"] for e in evidence):
        evidence.append({"kind": "pep", "pep": latest["pep"], "thread_id": None, "title": latest["title"],
                         "match": "supersedes_it", "url": latest["url"], "score": None})
    evidence += [x for x in extra if not any(e["pep"] == x["pep"] for e in evidence)]
    return {"topic": topic, "mode": mode, "action": action, "reason": reason,
            "confidence": max((r["score"] for r in rows), default=None),
            "evidence": evidence, "reviewers": who, "inactive": dropped, "draft": draft, "steps": tr.steps,
            "paths": tr.paths[:12], "cypher": tr.cypher}


def follow_chain(tr: Trace, n: int, hint: str, when: str) -> dict | None:
    """is_current on PEP n: the latest PEP in its SUPERSEDES chain, if one existed before the topic."""
    ic = tr.call("is_current", {"pep": str(n)},
                 f"{hint}: follow the SUPERSEDES chain to the PEP that describes Python now before pointing at it")
    row = (ic.get("rows") or [{}])[0] if ic["outcome"] == "answered" else {}
    if not row.get("latest"):
        tr.found("current: nothing supersedes it" if row.get("current") else f"nothing supersedes it ({row.get('status')})")
        return None
    latest = pep_brief(row["latest"][0]["pep"])
    if (latest["created"] or "")[:10] >= when:   # the topic can only be pointed at what existed before it
        tr.found(f"superseded by PEP {latest['pep']}, which is newer than the topic: not used")
        return None
    tr.found(f"superseded: latest in the chain is PEP {latest['pep']} ({latest['status']})")
    return latest


def decision_at(n: int) -> str | None:
    """When PEP n was decided (its earliest dated Decision), if the graph records it."""
    rows = graph().ro_query("""MATCH (:PEP {number: $n})-[:RESOLVED_BY]->(d:Decision) WHERE coalesce(d.at, '') <> ''
                               RETURN min(d.at)""", {"n": n}).result_set
    return rows[0][0] if rows else None


def cites(topic: dict, pep: dict) -> bool:
    """Does the topic name the PEP? Then it builds on the PEP, not repeats it. Its number anywhere ("PEP 616",
    "PEP-750", "pep-0616" in a link) counts; its full title counts only in the opening post and only when it is
    three words or more: a topic *titled* "Assignment expressions" is naming the idea, not citing PEP 572."""
    if re.search(rf"\bpep[\s_-]?0*{pep['pep']}\b", f"{topic.get('title') or ''}\n{topic.get('text') or ''}", re.I):
        return True
    title = " ".join((pep.get("title") or "").lower().split())
    return len(title.split()) >= 3 and title in " ".join((topic.get("text") or "").lower().split())


def builds_on(rows: list[dict], n: int) -> None:
    for r in rows:
        if r["kind"] == "pep" and r.get("pep") == n:
            r["match"] = "builds_on"


def scan_rejected_ideas(tr: Trace, target: dict, topic: dict) -> dict | None:
    """already_decided hides the rejected ideas of a PEP that matched itself. Score the adopted PEP's own rejected
    ideas against the topic with the same cross-encoder: the topic may be an idea the PEP set aside."""
    step = tr.local("rejected_ideas", {"pep": target["pep"]},
                    f"PEP {target['pep']} is {target['status']}, but an adopted PEP can still have set this exact idea "
                    "aside: score its own rejected ideas against the topic before saying it exists")
    alts = graph().ro_query("""MATCH (a:Alternative)-[:REJECTED_IN]->(:PEP {number: $n})
                               RETURN a.url, a.title, coalesce(a.text, '')""", {"n": target["pep"]}).result_set
    if not alts:
        step["outcome"] = "it records no rejected ideas"
        return None
    idea = tools.idea_text((topic["title"] + ". " + (topic.get("text") or "")[:240]).strip())
    scores = [round(float(x), 2) for x in tools._reranker().rerank(idea, [(t + ". " + x)[:tools.SNIPPET] for _, t, x in alts])]
    score, (url, title, text) = max(zip(scores, alts), key=lambda z: z[0])
    step["outcome"] = (f"{plural(len(alts), 'rejected idea')}; closest \"{title}\" at {score} "
                       + ("(same idea)" if score >= tools.SAME_IDEA else "(not the same idea)"))
    p = pep_brief(target["pep"])
    return {"kind": "rejected_idea", "match": "same", "score": score, "pep": target["pep"], "title": p["title"],
            "status": p["status"], "created": p["created"], "url": p["url"], "verdict": "set aside",
            "idea": title, "idea_url": url, "idea_quote": tools.first_sentences(text), "earlier_forms": [],
            "authors": pep_authors(target["pep"]), "decision": None, "reason": None, "superseded_by": None,
            "came_back": [], "earlier_tries": [], "quote": ""}


ACTIVE_YEARS = 3


def last_active(names: list[str], when: str) -> dict[str, tuple[str, str]]:
    """Each person's latest activity in the ACTIVE_YEARS before `when` (a date): a post in a forum thread created
    then, a PEP they authored or decided then, or a PR they opened then, as (date, what)."""
    if not names:
        return {}
    start = f"{int(when[:4]) - ACTIVE_YEARS}{when[4:10]}"
    rows = graph().ro_query("""
        UNWIND $names AS name MATCH (a:Person {name: name})
        OPTIONAL MATCH (a)-[:POSTED_IN|STARTED]->(t:Thread) WHERE t.created >= $start AND t.created < $when
        WITH a, max(t.created) AS posted
        OPTIONAL MATCH (a)-[:AUTHORED|DELEGATE_FOR]->(p:PEP) WHERE p.created >= $start AND p.created < $when
        WITH a, posted, p ORDER BY p.created DESC
        WITH a, posted, collect(p)[0] AS pep
        OPTIONAL MATCH (d:Decision)-[:MADE_BY]->(a) WHERE d.at >= $start AND d.at < $when
        WITH a, posted, pep, max(d.at) AS decided
        OPTIONAL MATCH (a)-[:OPENED]->(x:PR) WHERE x.created_at >= $start AND x.created_at < $when
        RETURN a.name, posted, pep.created, pep.number, decided, max(x.created_at)""",
                            {"names": names, "start": start, "when": when}).result_set
    out = {}
    for name, posted, pep_at, pep_n, decided, pr_at in rows:
        seen = [(posted, "posted on the forum"), (pep_at, f"authored PEP {pep_n}"), (decided, "decided a PEP"),
                (pr_at, "opened a CPython PR")]
        seen = [(d, what) for d, what in seen if d]
        if seen:
            out[name] = max(seen)
    return out


def active_only(tr: Trace, who: list[dict], when: str, limit: int = 3) -> tuple[list[dict], list[str]]:
    """Route only to people active in the ACTIVE_YEARS before the topic; each kept person's why says how."""
    if not who:
        return [], []
    seen = last_active([w["name"] for w in who], when)
    kept = [{**w, "why": f"{w['why']}; {seen[w['name']][1]} in {seen[w['name']][0][:4]}"} for w in who if w["name"] in seen][:limit]
    dropped = [w["name"] for w in who if w["name"] not in seen]
    tr.local("active_reviewers", {"people": [w["name"] for w in who]},
             f"route only to people active in the {ACTIVE_YEARS} years before the topic (forum posts in threads from "
             "then, PEPs authored or decided, PRs opened): an old author may have left")["outcome"] = (
        f"kept {len(kept)}" + (f"; dropped as inactive: {', '.join(dropped)}" if dropped else ""))
    return kept, dropped


def pep_authors(n: int) -> list[str]:
    return [r[0] for r in graph().ro_query("MATCH (a:Person)-[:AUTHORED]->(:PEP {number: $n}) RETURN a.name",
                                           {"n": n}).result_set]


def history(topic: int | None = None, limit: int = 20, mode: str | None = None, action: str | None = None) -> list[dict]:
    """Recorded decisions, newest first: what was decided, why, the evidence (PEPs, then earlier forum
    threads), who it went to and why, the drafted reply, the steps the agent took, and the links it wrote
    into the graph (SAME_IDEA_AS / RELATED_TO from the topic's thread)."""
    rows = graph().ro_query("""MATCH (tr:Triage)-[:ABOUT]->(th:Thread)
                               WHERE ($t IS NULL OR tr.topic = $t) AND ($m IS NULL OR coalesce(tr.mode, 'live') = $m)
                                     AND ($a IS NULL OR tr.action = $a)
                               OPTIONAL MATCH (tr)-[:EVIDENCE]->(p:PEP)
                               WITH tr, th, collect(DISTINCT p) AS ps
                               OPTIONAL MATCH (tr)-[:EVIDENCE]->(et:Thread)
                               WITH tr, th, ps, collect(DISTINCT et) AS ts
                               OPTIONAL MATCH (th)-[w:SAME_IDEA_AS|RELATED_TO]->(x) WHERE w.triage = tr.id
                               WITH tr, th, ps, ts, collect(DISTINCT [type(w), labels(x)[0], coalesce(x.number, x.id), x.url]) AS ws
                               OPTIONAL MATCH (tr)-[e:ROUTED_TO]->(who:Person)
                               RETURN tr.topic, th.title, th.url, tr.at, tr.action, tr.reason, tr.draft,
                                      [p IN ps | [p.number, p.title]], [x IN ts | [x.id, x.title, x.url]],
                                      collect({name: who.name, why: e.why}), coalesce(tr.mode, 'live'), tr.steps, ws
                               ORDER BY tr.at DESC LIMIT $n""", {"t": topic, "n": limit, "m": mode, "a": action}).result_set
    out = []
    for topic_id, title, url, at, act, reason, draft, ps, ts, routed, md, steps, ws in rows:
        ps, routed = sorted(ps), [x for x in routed if x["name"]]
        wrote = sorted(({"rel": rel, "to": f"PEP {key}" if lbl == "PEP" else f"thread {key}",
                         "url": u or (f"https://peps.python.org/pep-{key:04d}/" if lbl == "PEP" else f"https://discuss.python.org/t/{key}")}
                        for rel, lbl, key, u in ws if rel), key=lambda w: (w["rel"] != "SAME_IDEA_AS", w["to"]))
        out.append({"topic": topic_id, "title": title, "url": url, "at": at, "action": act, "reason": reason,
                    "evidence_peps": [n for n, _ in ps], "routed_to": [x["name"] for x in routed],
                    "evidence": [{"kind": "pep", "id": n, "title": t, "url": f"https://peps.python.org/pep-{n:04d}/"}
                                 for n, t in ps]
                                + [{"kind": "thread", "id": i, "title": t, "url": u} for i, t, u in sorted(ts)],
                    "routed": routed, "draft": draft, "mode": md, "steps": json.loads(steps) if steps else [],
                    "wrote": wrote})
    return out


def already_triaged(tid: int) -> bool:
    return bool(graph().ro_query("MATCH (t:Triage {topic: $t}) RETURN t LIMIT 1", {"t": tid}).result_set)


def triaged_topics() -> set[int]:
    return {r[0] for r in graph().ro_query("MATCH (t:Triage) RETURN DISTINCT t.topic").result_set}


def forget(tid: int) -> int:
    """Remove the triage record of one topic: its Triage nodes and the links triage wrote from its thread
    (the thread itself stays). Returns how many decisions were removed."""
    g = graph()
    g.query("""MATCH (th:Thread {id: $t})-[w:SAME_IDEA_AS|RELATED_TO]->() WHERE w.by = 'triage' DELETE w""", {"t": tid})
    (n,), = g.query("MATCH (tr:Triage {topic: $t}) WITH collect(tr) AS trs UNWIND trs AS tr DETACH DELETE tr "
                    "RETURN size(trs)", {"t": tid}).result_set or [[0]]
    return n


def write_back(g, tid: str, topic_id: int, at: str, evidence: list[dict]) -> list[dict]:
    """Link the topic's thread to what it repeats (SAME_IDEA_AS) or touches (RELATED_TO). Only targets
    that exist in the graph are linked; the links carry the triage id, so they can be traced and removed."""
    written = []
    for match, rel in WRITES.items():
        peps = [{"n": e["pep"], "c": e.get("score")} for e in evidence
                if e["match"] == match and e["kind"] != "thread" and isinstance(e.get("pep"), int)]
        threads = [{"n": e["thread_id"], "c": e.get("score")} for e in evidence
                   if e["match"] == match and e["kind"] == "thread" and isinstance(e.get("thread_id"), int)
                   and e["thread_id"] != topic_id]
        for target, key, items in (("PEP", "number", peps), ("Thread", "id", threads)):
            best: dict[int, float | None] = {}   # two rejected ideas of one PEP are one link, at the higher score
            for i in items:
                best[i["n"]] = max((c for c in (best.get(i["n"]), i["c"]) if c is not None), default=None)
            items = [{"n": n, "c": c} for n, c in best.items()]
            if not items:
                continue
            got = g.query(f"""MATCH (th:Thread {{id: $t}}) UNWIND $rows AS r MATCH (x:{target} {{{key}: r.n}})
                              MERGE (th)-[w:{rel}]->(x)
                              SET w.by = 'triage', w.triage = $id, w.confidence = r.c, w.at = $at, w.weight = 1.0
                              RETURN x.{key}""", {"t": topic_id, "rows": items, "id": tid, "at": at}).result_set
            written += [{"rel": rel, "to": f"{'PEP' if target == 'PEP' else 'thread'} {r[0]}"} for r in got]
    return written


def record(d: dict) -> str:
    """Act: write the decision into the graph, link the topic's thread to what it repeats, then write the
    outbox (and a webhook, if one is configured; live runs only)."""
    t, at = d["topic"], datetime.now(timezone.utc).isoformat(timespec="seconds")
    mode = d.get("mode", "live")
    tid = f"triage-{t['id']}-{at}"
    g = graph()
    g.query("""MERGE (th:Thread {id: $t}) ON CREATE SET th.title = $title, th.url = $url, th.created = $created, th.kind = 'idea'
               CREATE (tr:Triage {id: $id, topic: $t, at: $at, mode: $mode, action: $action, reason: $reason,
                                  confidence: $conf, draft: $draft, steps: $steps})
               CREATE (tr)-[:ABOUT {weight: 1.0}]->(th)""",
            {"t": t["id"], "title": t["title"], "url": t["url"], "created": t.get("created") or "", "id": tid, "at": at,
             "mode": mode, "action": d["action"], "reason": d["reason"], "conf": d["confidence"], "draft": d.get("draft"),
             "steps": json.dumps(d.get("steps") or [])})
    peps = sorted({e["pep"] for e in d["evidence"] if isinstance(e.get("pep"), int)})
    if peps:
        g.query("""MATCH (tr:Triage {id: $id}) UNWIND $peps AS n MATCH (p:PEP {number: n})
                   MERGE (tr)-[:EVIDENCE {weight: 1.0}]->(p)""", {"id": tid, "peps": peps})
    threads = sorted({e["thread_id"] for e in d["evidence"] if e["kind"] == "thread" and isinstance(e.get("thread_id"), int)})
    if threads:
        g.query("""MATCH (tr:Triage {id: $id}) UNWIND $threads AS n MATCH (th:Thread {id: n})
                   MERGE (tr)-[:EVIDENCE {weight: 1.0}]->(th)""", {"id": tid, "threads": threads})
    if d["reviewers"]:
        g.query("""MATCH (tr:Triage {id: $id}) UNWIND $people AS r MATCH (p:Person {name: r.name})
                   MERGE (tr)-[e:ROUTED_TO]->(p) SET e.why = r.why, e.weight = 1.0""",
                {"id": tid, "people": d["reviewers"]})
    d["wrote"] = write_back(g, tid, t["id"], at, d["evidence"])
    box = OUTBOX / "backfill" if mode == "backfill" else OUTBOX
    box.mkdir(parents=True, exist_ok=True)
    (box / f"{t['id']}.json").write_text(json.dumps({k: v for k, v in d.items() if k != "paths"}, indent=1, default=str))
    (box / f"{t['id']}.md").write_text(outbox_text(d))
    if mode == "live" and (url := os.getenv("TRIAGE_WEBHOOK_URL")):   # e.g. a Slack or Discord incoming webhook; off by default
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
    if d.get("steps"):
        lines += ["**How the agent got there:**"] + [f"{i}. `{s['tool']}`: {s['why']}\n   Found: {s['outcome']}"
                                                     for i, s in enumerate(d["steps"], 1)] + [""]
    if d.get("wrote"):
        lines += ["**Written to the graph:** " + "; ".join(f"{w['rel']} -> {w['to']}" for w in d["wrote"]), ""]
    if d["draft"]:
        lines += ["**Drafted reply (not posted):**", "", d["draft"]]
    return "\n".join(lines) + "\n"


def triage(topics: list[dict], dry_run: bool = False, redo: bool = False) -> list[dict]:
    out = []
    for t in topics:
        if redo and not dry_run:
            forget(t["id"])
        if not dry_run and already_triaged(t["id"]):
            print(f"skip   {t['id']}  {t['title'][:70]}  (already triaged)")
            continue
        d = decide(t)
        if not dry_run:
            d["triage_id"] = record(d)
        print(f"{d['action']:<19} {t['id']}  {t['title'][:70]}")
        out.append(d)
    return out


def backfill(since: str, limit: int | None = None, dry_run: bool = False, path: Path = RAW) -> list[dict]:
    """Triage cached Ideas topics created since `since`, oldest first, as mode='backfill'. Topics already
    triaged are skipped (also in a dry run), and each decision is written as soon as it is made, so a run that
    stops resumes where it left off. Nothing is posted and no webhook is called."""
    done, out, skipped, failed = triaged_topics(), [], 0, 0
    for t in cached_topics(since, path):
        if t["id"] in done:
            skipped += 1
            continue
        if limit is not None and len(out) >= limit:
            break
        try:
            d = decide(t, mode="backfill")
            if not dry_run:
                d["triage_id"] = record(d)
        except Exception as e:   # one bad topic must not stop a long run; it is retried on the next run
            failed += 1
            print(f"error  {t['id']}  {t['title'][:70]}  ({type(e).__name__}: {e})")
            continue
        print(f"{d['action']:<19} {t['id']}  {t['created'][:10]}  {t['title'][:60]}")
        out.append(d)
    counts = {a: sum(d["action"] == a for d in out) for a in ACTIONS}
    print(f"backfill since {since}: {len(out)} {'decided (dry run, nothing written)' if dry_run else 'recorded'}, "
          f"{skipped} already triaged, {failed} failed; " + ", ".join(f"{a} {n}" for a, n in counts.items() if n))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--latest", type=int, help="triage the N newest Ideas topics")
    ap.add_argument("--topic", type=int, action="append", help="triage one topic id (repeatable)")
    ap.add_argument("--redo", action="store_true", help="with --topic/--latest: forget earlier decisions first")
    ap.add_argument("--backfill", action="store_true", help="triage cached Ideas topics (data/raw/discourse.json)")
    ap.add_argument("--since", default="2024-01-01", help="with --backfill: topics created on or after this date")
    ap.add_argument("--limit", type=int, help="with --backfill: at most N new decisions this run")
    ap.add_argument("--dry-run", action="store_true", help="decide only; write nothing")
    a = ap.parse_args()
    if a.backfill:
        backfill(a.since, a.limit, a.dry_run)
        return
    topics = [x for tid in (a.topic or []) if (x := fetch_topic(tid))] + (latest_topics(a.latest) if a.latest else [])
    triage(topics, a.dry_run, a.redo)


if __name__ == "__main__":
    main()
