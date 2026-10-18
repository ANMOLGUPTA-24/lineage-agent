"""Triage agent tests against the loaded graph (run `make ingest` first). Topics are synthetic (negative ids,
no network); the round trip writes one fake topic into the graph and the outbox and removes it again."""
import json
import re

import pytest

from lineage import triage
from lineage.db import connect, graph, resolve

TODAY = "2026-10-17"


@pytest.fixture(scope="module", autouse=True)
def graph_loaded():
    if resolve("python") not in connect().list_graphs():
        pytest.skip("graph not loaded")


def topic(title, tid=-1, created=TODAY, text=""):
    return {"id": tid, "title": title, "created": created, "url": f"https://discuss.python.org/t/{tid}", "text": text}


def peps(d):
    return {e["pep"] for e in d["evidence"] if isinstance(e.get("pep"), int)}


def test_rejected_idea_gets_the_ruling():
    d = triage.decide(topic("Add a do-while loop to Python"))
    assert d["action"] == "reply_with_ruling" and 315 in peps(d)
    assert "PEP 315" in d["reason"] and "https://peps.python.org/pep-0315/" in d["draft"]
    assert any(r["why"].startswith("author of PEP 315; ") for r in d["reviewers"])


def test_adopted_idea_points_to_existing():
    d = triage.decide(topic("Assignment expressions: allow name := value inside expressions", tid=-2))
    assert d["action"] == "point_to_existing" and 572 in peps(d)
    assert "Final" in d["reason"]


def test_open_draft_pep_is_not_a_ruling():
    d = triage.decide(topic("Docstrings for type aliases", tid=-5))   # PEP 846 is a Draft
    assert d["action"] == "link_prior_threads" and 846 in peps(d)
    assert "open PEP (846" in d["reason"] and "join that discussion" in d["reason"]
    assert d["draft"].startswith("An open PEP already proposes this: PEP 846")


def test_same_day_pep_is_not_prior_history():
    # PEP 846 was created on 2026-09-06, the day its Ideas topic was posted: it is that topic's PEP.
    d = triage.decide(topic("Docstrings for type aliases", tid=-6, created="2026-09-06"))
    assert 846 not in peps(d)


def test_latest_topics_are_newest_created(monkeypatch):
    from lineage.ingest import discourse
    asked = []
    listing = [{"id": 1, "created_at": "2026-10-01", "pinned": True}, {"id": 2, "created_at": "2026-10-10"},
               {"id": 3, "created_at": "2026-10-16"}, {"id": 4, "created_at": "2026-10-12"}]
    monkeypatch.setattr(discourse, "get", lambda path: asked.append(path) or {"topic_list": {"topics": listing}})
    monkeypatch.setattr(triage, "fetch_topic", lambda tid: {"id": tid})
    assert [t["id"] for t in triage.latest_topics(2)] == [3, 4]
    assert "order=created" in asked[0]


def test_pagerank_reviewers_only_for_a_same_idea_pep():
    def row(pep, match):
        return {"kind": "pep", "pep": pep, "match": match, "authors": ["Martin von Löwis"] if pep == 3120 else []}
    loose = triage.reviewers([row(3120, "related")])   # its area is a generic "cluster around PEP N"
    assert loose == [{"name": "Martin von Löwis", "why": "author of PEP 3120"}]
    assert any("PageRank" in r["why"] for r in triage.reviewers([row(3120, "same")]))


def test_reasons_are_pluralized_and_say_when_nobody_qualifies(monkeypatch):
    related = {"kind": "thread", "thread_id": 7, "created": "2020-01-01", "match": "related", "score": 2.0,
               "title": "t", "url": "u", "participants": []}
    monkeypatch.setattr(triage.tools, "call", lambda name, args: {"outcome": "answered", "rows": [related]})
    d = triage.decide(topic("anything", tid=-7))
    assert d["action"] == "flag_for_review" and "(1 item)" in d["reason"]
    assert d["reviewers"] == [] and "Nobody in the evidence qualifies" in d["reason"]
    assert triage.plural(3, "item") == "3 items"


def test_unrecorded_idea_is_routed_with_no_evidence():
    d = triage.decide(topic("A banana keyword for fruit-based control flow", tid=-3))
    assert d["action"] == "route_to_experts"
    assert d["evidence"] == [] and d["draft"] is None and d["confidence"] is None
    assert "not proof" in d["reason"]
    # Whoever it is routed to comes from a nearby earlier thread, and says so.
    assert all("not a match" in r["why"] for r in d["reviewers"])


def test_prior_drops_own_thread_and_newer_items():
    t = topic("x", tid=500, created="2024-05-01")
    rows = [{"kind": "thread", "thread_id": 500, "created": "2024-05-01", "match": "same"},      # its own thread
            {"kind": "thread", "thread_id": 501, "created": "2024-06-01", "match": "same"},      # newer thread
            {"kind": "thread", "thread_id": 502, "created": "2024-05-01", "match": "same"},      # same day: not before it
            {"kind": "thread", "thread_id": 503, "created": None, "match": "same"},              # undated thread
            {"kind": "thread", "thread_id": 504, "created": "2020-01-01", "match": "same"},      # earlier: kept
            {"kind": "pep", "pep": 1, "created": "2025-01-01", "match": "same"},                 # newer PEP
            {"kind": "pep", "pep": 9, "created": "2024-05-01", "match": "same"},                 # same day: its own PEP
            {"kind": "pep", "pep": 2, "created": "2010-01-01", "match": "same",                   # earlier PEP: kept,
             "came_back": [{"pep": 3, "created": "2025-01-01"}, {"pep": 4, "created": "2012-01-01"}],
             "earlier_tries": [{"pep": 5, "created": "2008-01-01"}]}]                             # minus later comebacks
    kept = triage.prior(rows, t)
    assert [r.get("thread_id") or r.get("pep") for r in kept] == [504, 2]
    assert [b["pep"] for b in kept[1]["came_back"]] == [4] and [e["pep"] for e in kept[1]["earlier_tries"]] == [5]
    assert len(rows[-1]["came_back"]) == 2   # the caller's rows are not modified


def test_ruling_draft_never_cites_a_later_pep():
    when = "2015-01-01"   # PEP 472 (2014) was argued again in PEPs 637, 647 and 696, all later than this
    d = triage.decide(topic("Support index keyword arguments in subscripts: d[x=1]", tid=-4, created=when))
    assert d["action"] == "reply_with_ruling" and 472 in peps(d)
    cited = {int(n) for n in re.findall(r"PEP (\d+)", d["draft"] + d["reason"])}
    created = dict(graph().ro_query("MATCH (p:PEP) WHERE p.number IN $n RETURN p.number, p.created",
                                    {"n": sorted(cited)}).result_set)
    assert 472 in cited and all(created[n][:10] <= when for n in cited)


FAKE = 990000001


@pytest.fixture
def fake_topic(monkeypatch):
    monkeypatch.delenv("TRIAGE_WEBHOOK_URL", raising=False)
    g = graph()
    assert not g.ro_query("MATCH (t:Thread {id: $t}) RETURN t", {"t": FAKE}).result_set, "fake id collides"
    yield topic("Lineage triage test topic (fake)", tid=FAKE)
    g.query("MATCH (tr:Triage {topic: $t}) DETACH DELETE tr", {"t": FAKE})
    g.query("MATCH (th:Thread {id: $t}) DETACH DELETE th", {"t": FAKE})
    for ext in ("md", "json"):
        (triage.OUTBOX / f"{FAKE}.{ext}").unlink(missing_ok=True)
        (triage.OUTBOX / "backfill" / f"{FAKE}.{ext}").unlink(missing_ok=True)


EARLIER = 65193   # a real forum thread ("New function `repr_args` in `pprint` or `reprlib`")


def fake_decision(t):
    return {"topic": t, "action": "reply_with_ruling", "reason": "test reason", "confidence": 7.5,
            "evidence": [{"kind": "pep", "pep": 315, "thread_id": None, "title": "Enhanced While Loop", "match": "same",
                          "url": "https://peps.python.org/pep-0315/", "score": 7.5},
                         {"kind": "thread", "pep": None, "thread_id": EARLIER, "title": "x", "match": "related", "url": "u",
                          "score": 3.1}],
            "reviewers": [{"name": "Raymond Hettinger", "why": "author of PEP 315"}], "draft": "drafted text", "paths": [],
            "mode": "live", "steps": [{"tool": "already_decided", "args": {"idea": "x"}, "why": "start", "outcome": "1 match"}]}


def test_record_history_round_trip(fake_topic):
    assert not triage.already_triaged(FAKE)
    tid = triage.record(fake_decision(fake_topic))
    assert tid.startswith(f"triage-{FAKE}-") and triage.already_triaged(FAKE)
    (h,) = triage.history(topic=FAKE)
    assert h["action"] == "reply_with_ruling" and h["reason"] == "test reason" and h["title"] == fake_topic["title"]
    assert h["evidence_peps"] == [315] and h["routed_to"] == ["Raymond Hettinger"]
    (url,), = graph().ro_query("MATCH (t:Thread {id: $t}) RETURN t.url", {"t": EARLIER}).result_set
    assert h["evidence"] == [
        {"kind": "pep", "id": 315, "title": "Enhanced While Loop", "url": "https://peps.python.org/pep-0315/"},
        {"kind": "thread", "id": EARLIER, "title": "New function `repr_args` in `pprint` or `reprlib`", "url": url}]
    assert h["routed"] == [{"name": "Raymond Hettinger", "why": "author of PEP 315"}]
    assert h["draft"] == "drafted text"
    assert "drafted text" in (triage.OUTBOX / f"{FAKE}.md").read_text()
    assert (triage.OUTBOX / f"{FAKE}.json").exists()
    assert triage.triage([fake_topic]) == []   # the graph remembers: a second run skips it


def test_sync_carries_thread_evidence_and_draft(fake_topic):
    from lineage.ingest import sync
    triage.record(fake_decision(fake_topic))
    db, name = connect(), "triagecopytest"
    dst = db.select_graph(name)
    try:
        dst.query("""CREATE (:PEP {number: 315, title: 'Enhanced While Loop'}), (:Person {name: 'Raymond Hettinger'}),
                            (:Thread {id: $t, title: 'earlier', url: 'u'})""", {"t": EARLIER})
        (at,), = graph().ro_query("MATCH (t:Triage {topic: $t}) RETURN t.at", {"t": FAKE}).result_set
        assert sync.copy_triage(graph(), dst, since=at) >= 1
        (got,), = dst.ro_query("""MATCH (tr:Triage {topic: $t})
                                  RETURN [tr.draft, [(tr)-[:EVIDENCE]->(p:PEP) | p.number], [(tr)-[:EVIDENCE]->(x:Thread) | x.id],
                                          [(tr)-[e:ROUTED_TO]->(who:Person) | e.why]]""", {"t": FAKE}).result_set
        assert got == ["drafted text", [315], [EARLIER], ["author of PEP 315"]]
        (mode, steps), = dst.ro_query("MATCH (tr:Triage {topic: $t}) RETURN tr.mode, tr.steps", {"t": FAKE}).result_set
        assert mode == "live" and json.loads(steps)[0]["tool"] == "already_decided"
        links = dst.ro_query("""MATCH (:Thread {id: $t})-[w]->(x) WHERE w.by = 'triage'
                                RETURN type(w), coalesce(x.number, x.id), w.confidence ORDER BY type(w)""", {"t": FAKE}).result_set
        assert links == [["RELATED_TO", EARLIER, 3.1], ["SAME_IDEA_AS", 315, 7.5]]
    finally:
        dst.delete()


def test_unreachable_webhook_does_not_stop_the_record(fake_topic, monkeypatch, capsys):
    monkeypatch.setenv("TRIAGE_WEBHOOK_URL", "http://127.0.0.1:9/")
    d = {"topic": fake_topic, "action": "route_to_experts", "reason": "r", "confidence": None, "evidence": [],
         "reviewers": [], "draft": None, "paths": []}
    assert triage.record(d) and triage.already_triaged(FAKE)
    assert (triage.OUTBOX / f"{FAKE}.md").exists() and "webhook failed" in capsys.readouterr().out


# ---------------------------------------------------------------- the adaptive plan (synthetic rows, no network)

def pep_row(n, status, match="same", **kw):
    return {"kind": "pep", "pep": n, "match": match, "score": 7.0, "title": f"PEP {n} title", "status": status,
            "verdict": status.lower(), "created": "2010-01-01", "url": f"https://peps.python.org/pep-{n:04d}/",
            "quote": "q", "authors": ["Ann Author", "Bob Author"], "reason": None, "superseded_by": None,
            "came_back": [], "earlier_tries": [], "decision": {"outcome": status, "at": "2011", "url": "ruling-url",
                                                               "decided_by": [{"by": "Guido van Rossum", "inferred": False}]},
            **kw}


def thread_row(tid, match="same", committers=(), peps=()):
    return {"kind": "thread", "thread_id": tid, "match": match, "score": 6.0, "title": f"thread {tid}",
            "created": "2020-01-01", "url": f"https://discuss.python.org/t/{tid}", "posts": 10,
            "participants": [{"name": c, "posts": 5, "group": "committers"} for c in committers],
            "peps": [{"pep": n, "title": "t", "status": "Final", "how": "DISCUSSES"} for n in peps], "started_by": "x"}


def fake_tools(monkeypatch, rows, **replies):
    """tools.call answering already_decided with `rows` and any other tool from `replies` (a function of args)."""
    calls = []

    def call(name, args):
        calls.append((name, args))
        if name == "already_decided":
            return {"outcome": "answered" if rows else "not_in_graph", "rows": rows, "paths": [], "cypher": ["q"]}
        return replies[name](args) if name in replies else {"outcome": "not_in_graph", "rows": [], "paths": []}
    monkeypatch.setattr(triage.tools, "call", call)
    monkeypatch.setattr(triage, "earlier_triage", lambda ids: [])
    monkeypatch.setattr(triage, "decision_at", lambda n: decided_on.get(n))
    monkeypatch.setattr(triage, "pep_authors", lambda n: ["Ann Author", "Bob Author"])
    monkeypatch.setattr(triage, "last_active", lambda names, when: {n: ("2026-01-02", "posted on the forum")
                                                                    for n in names if n not in inactive})

    def scan(tr, target, topic):
        tr.local("rejected_ideas", {"pep": target["pep"]}, "adopted PEP")["outcome"] = "none close"
        return alts.get(target["pep"])
    monkeypatch.setattr(triage, "scan_rejected_ideas", scan)
    return calls


decided_on: dict = {}   # PEP -> decision date, for the synthetic planner tests (reset by the autouse fixture)
alts: dict = {}         # PEP -> the rejected idea scan_rejected_ideas finds
inactive: set = set()   # people last_active does not find


@pytest.fixture(autouse=True)
def reset_fakes():
    decided_on.clear(), alts.clear(), inactive.clear()
    yield


def tools_of(d):
    assert all(s["why"] and s["outcome"] for s in d["steps"]), d["steps"]   # every step says why and what it found
    return [s["tool"] for s in d["steps"] if s["tool"] != "active_reviewers"]   # tested on its own below


def test_plan_superseded_pep_points_at_the_latest(monkeypatch):
    fake_tools(monkeypatch, [pep_row(601, "Rejected", superseded_by="PEP 765")],
               is_current=lambda a: {"outcome": "answered", "rows": [{"pep": 601, "current": False,
                                                                       "latest": [{"pep": 765, "status": "Final"}]}]})
    monkeypatch.setattr(triage, "pep_brief", lambda n: {"pep": n, "title": "Disallow it", "status": "Final",
                                                        "url": "https://peps.python.org/pep-0765/", "created": "2024-11-15"})
    d = triage.decide(topic("Forbid return in finally", tid=-10))
    assert tools_of(d) == ["already_decided", "is_current", "rejected_ideas", "draft_reply"]   # a ruling is on record: no why()
    assert "superseded by PEP 765" in d["steps"][1]["why"] and "PEP 765 (Final)" in d["steps"][1]["outcome"]
    assert d["action"] == "point_to_existing" and "superseded by PEP 765" in d["reason"]
    assert d["draft"].split("\n")[2].startswith("What Python follows now is PEP 765")   # near the top
    assert {e["pep"]: e["match"] for e in d["evidence"]} == {601: "same", 765: "supersedes_it"}


def test_plan_asks_why_only_when_the_ruling_is_missing(monkeypatch):
    calls = fake_tools(monkeypatch, [pep_row(505, "Deferred", decision=None,
                                             came_back=[{"pep": 824, "created": "2008-01-01", "status": "Draft",
                                                         "url": "u", "title": "t"}])],
                       why=lambda a: {"outcome": "answered", "rows": [{"pep": 505, "decision": None}]})
    d = triage.decide(topic("None-aware operators", tid=-11))
    assert tools_of(d) == ["already_decided", "why", "draft_reply"] and ("why", {"pep": "505"}) in calls
    assert "no Decision or decider" in d["steps"][1]["why"]
    assert d["action"] == "reply_with_ruling" and "only its status (Deferred)" in d["reason"]
    assert "came up again in 1 later PEP: PEP 824" in d["reason"]
    fake_tools(monkeypatch, [pep_row(315, "Rejected")])   # decided by someone: no why()
    assert tools_of(triage.decide(topic("do-while", tid=-12))) == ["already_decided", "draft_reply"]


def test_plan_adopted_pep_is_checked_for_currency(monkeypatch):
    fake_tools(monkeypatch, [pep_row(572, "Final")],
               is_current=lambda a: {"outcome": "answered", "rows": [{"pep": 572, "current": True, "latest": []}]})
    d = triage.decide(topic("walrus", tid=-13))
    assert tools_of(d) == ["already_decided", "rejected_ideas", "is_current", "draft_reply"]
    assert d["action"] == "point_to_existing" and d["steps"][2]["outcome"] == "current: nothing supersedes it"


def test_plan_same_threads_use_their_participants(monkeypatch):
    fake_tools(monkeypatch, [thread_row(11, committers=["Core One", "Core Two"])])
    monkeypatch.setattr(triage, "earlier_triage", lambda ids: [
        {"thread_id": 11, "thread": "thread 11", "pep": 315, "title": "Enhanced While Loop", "status": "Rejected",
         "created": "2003-04-22", "at": "2026-10-01"}])
    monkeypatch.setattr(triage, "pep_authors", lambda n: ["Raymond Hettinger"])
    monkeypatch.setattr(triage, "pep_brief", lambda n: {"pep": n, "url": "https://peps.python.org/pep-0315/"})
    d = triage.decide(topic("loop until", tid=-14))
    assert tools_of(d) == ["already_decided", "earlier_triage", "draft_reply"]   # committers posted: no who_knows
    assert d["action"] == "link_prior_threads" and [r["name"] for r in d["reviewers"]][:2] == ["Core One", "Core Two"]
    assert "earlier triage linked \"thread 11\" to PEP 315" in d["reason"]
    assert any(e["pep"] == 315 and e["match"] == "via_triage" for e in d["evidence"])
    assert d["reviewers"][-1] == {"name": "Raymond Hettinger",
                                  "why": "author of PEP 315, which an earlier triage linked to this idea; "
                                         "posted on the forum in 2026"}


def test_plan_threads_without_core_devs_ask_who_knows_their_pep(monkeypatch):
    calls = fake_tools(monkeypatch, [thread_row(12, peps=[695])],
                       who_knows=lambda a: {"outcome": "answered", "rows": [{"subject": "PEP 695",
                                                                             "direct": [{"name": "Eric Traut", "role": "authored"}]}]})
    d = triage.decide(topic("generic syntax", tid=-15))
    assert tools_of(d) == ["already_decided", "earlier_triage", "who_knows", "draft_reply"]
    assert ("who_knows", {"subject": "695"}) in calls and "is the discussion of PEP 695" in \
        next(s["why"] for s in d["steps"] if s["tool"] == "who_knows")
    assert d["reviewers"] == [{"name": "Eric Traut",
                               "why": "author of PEP 695, which \"thread 12\" discusses; posted on the forum in 2026"}]


def test_plan_related_history_checks_reviewers_with_connection(monkeypatch):
    hops = {"Near Dev": 1, "Far Dev": 5}
    calls = fake_tools(monkeypatch, [pep_row(3120, "Final", match="related", authors=[]),
                                     thread_row(13, match="related", committers=["Far Dev", "Near Dev"])],
                       connection=lambda a: {"outcome": "answered", "rows": [{"hops": hops[a["a"]], "strength": "x"}]},
                       who_knows=lambda a: {"outcome": "answered", "rows": [{"subject": "the cluster around PEP 1",
                                                                             "most_built_on": [{"name": "Hub", "rank": 1}]}]})
    d = triage.decide(topic("source encodings", tid=-16))
    assert d["action"] == "flag_for_review" and d["draft"] is None
    assert tools_of(d) == ["already_decided", "connection", "connection"]   # 2 reviewers already: no who_knows
    assert [a for n, a in calls if n == "connection"] == [{"a": "Far Dev", "b": "PEP 3120"}, {"a": "Near Dev", "b": "PEP 3120"}]
    assert [r["name"] for r in d["reviewers"]] == ["Near Dev", "Far Dev"]   # the closer one first
    assert d["reviewers"][0]["why"].endswith("1 hop from PEP 3120 in the graph; posted on the forum in 2026")


def test_plan_generic_cluster_is_not_used_for_related_history(monkeypatch):
    fake_tools(monkeypatch, [pep_row(3120, "Final", match="related", authors=["Only Author"])],
               who_knows=lambda a: {"outcome": "answered", "rows": [{"subject": "the cluster around PEP 1",
                                                                     "most_built_on": [{"name": "Hub", "rank": 1}]}]})
    d = triage.decide(topic("source encodings", tid=-17))
    assert tools_of(d) == ["already_decided", "who_knows"] and "generic cluster" in d["steps"][1]["outcome"]
    assert d["reviewers"] == [{"name": "Only Author", "why": "author of PEP 3120; posted on the forum in 2026"}]


def test_plan_nothing_found_goes_to_nearest_threads_then_who_knows(monkeypatch):
    fake_tools(monkeypatch, [], who_knows=lambda a: {"outcome": "answered", "rows": [{"subject": "PEP 8000",
                                                                                     "direct": [{"name": "Gov", "role": "delegate_for"}]}]})
    monkeypatch.setattr(triage, "nearest_threads", lambda t: [{"id": 1, "title": "near", "committers": ["Dev A"], "peps": [8000]}])
    d = triage.decide(topic("something new", tid=-18))
    assert tools_of(d) == ["already_decided", "nearest_threads", "who_knows"]
    assert d["action"] == "route_to_experts" and d["draft"] is None and d["evidence"] == []
    assert [r["name"] for r in d["reviewers"]] == ["Dev A", "Gov"] and all("not a match" in r["why"] for r in d["reviewers"])
    assert d["reviewers"][1]["why"].startswith("decided PEP 8000")


def test_plan_on_the_real_graph_follows_supersession():
    d = triage.decide(topic("Forbid return, break and continue breaking out of finally", tid=-19))
    assert tools_of(d)[:3] == ["already_decided", "is_current", "rejected_ideas"] and d["action"] == "point_to_existing"
    assert "superseded by PEP 765" in d["reason"] and 765 in peps(d)


# ---------------------------------------------------------------- write-back, modes, backfill, API

def test_write_back_links_the_topic_and_history_reports_it(fake_topic):
    from lineage import tools
    run = tools.Run("python")
    before = tools.thread_context(run, EARLIER)[0]
    tid = triage.record(fake_decision(fake_topic))
    links = graph().ro_query("""MATCH (:Thread {id: $t})-[w]->(x) WHERE w.by = 'triage'
                                RETURN type(w), coalesce(x.number, x.id), w.triage, w.confidence ORDER BY type(w)""",
                             {"t": FAKE}).result_set
    assert links == [["RELATED_TO", EARLIER, tid, 3.1], ["SAME_IDEA_AS", 315, tid, 7.5]]
    (h,) = triage.history(topic=FAKE)
    (url,), = graph().ro_query("MATCH (t:Thread {id: $t}) RETURN t.url", {"t": EARLIER}).result_set
    assert h["wrote"] == [{"rel": "SAME_IDEA_AS", "to": "PEP 315", "url": "https://peps.python.org/pep-0315/"},
                          {"rel": "RELATED_TO", "to": f"thread {EARLIER}", "url": url}]
    assert h["mode"] == "live" and h["steps"] == fake_decision(fake_topic)["steps"]
    assert "**Written to the graph:** SAME_IDEA_AS -> PEP 315" in (triage.OUTBOX / f"{FAKE}.md").read_text()
    # The links do not change what already_decided reads about the earlier thread.
    assert tools.thread_context(run, EARLIER)[0] == before
    assert triage.forget(FAKE) == 1 and not triage.already_triaged(FAKE)
    assert not graph().ro_query("MATCH (:Thread {id: $t})-[w]->() WHERE w.by = 'triage' RETURN w", {"t": FAKE}).result_set


def test_backfill_mode_is_recorded_and_filtered(fake_topic, monkeypatch, capsys):
    monkeypatch.setenv("TRIAGE_WEBHOOK_URL", "http://127.0.0.1:9/")   # backfill never notifies
    triage.record({**fake_decision(fake_topic), "mode": "backfill"})
    assert "webhook" not in capsys.readouterr().out
    assert (triage.OUTBOX / "backfill" / f"{FAKE}.md").exists() and not (triage.OUTBOX / f"{FAKE}.md").exists()
    assert [h["topic"] for h in triage.history(mode="backfill", limit=100)].count(FAKE) == 1
    assert FAKE not in [h["topic"] for h in triage.history(mode="live", limit=100)]
    assert all(h["action"] == "reply_with_ruling" for h in triage.history(action="reply_with_ruling", limit=100))


def cache_file(tmp_path, ideas, texts=None):
    path = tmp_path / "discourse.json"
    path.write_text(json.dumps({"ideas": {str(i["id"]): i for i in ideas},
                                "topics": {str(k): {"text": v} for k, v in (texts or {}).items()}}))
    return path


def test_cached_topics_and_backfill_resume(tmp_path, monkeypatch):
    ideas = [{"id": 20, "title": "About the Ideas category", "created": "2025-01-01T00:00:00Z"},
             {"id": 3, "title": "c", "created": "2024-03-01T00:00:00Z"}, {"id": 1, "title": "a", "created": "2023-12-31T00:00:00Z"},
             {"id": 2, "title": "b", "created": "2024-02-01T00:00:00Z"}, {"id": 4, "title": "d", "created": "2024-04-01T00:00:00Z"}]
    path = cache_file(tmp_path, ideas, {2: "opening of b"})
    got = triage.cached_topics("2024-01-01", path)
    assert [t["id"] for t in got] == [2, 3, 4] and got[0]["text"] == "opening of b" and got[1]["text"] == ""
    monkeypatch.setattr(triage, "triaged_topics", lambda: {2})   # done in an earlier run: skipped, not counted
    monkeypatch.setattr(triage, "decide", lambda t, mode: {"topic": t, "action": "route_to_experts", "mode": mode})
    out = triage.backfill("2024-01-01", limit=1, dry_run=True, path=path)
    assert [(d["topic"]["id"], d["mode"]) for d in out] == [(3, "backfill")]


def test_backfill_dry_run_on_two_cached_topics(tmp_path):
    cache = json.loads(triage.RAW.read_text())
    done = triage.triaged_topics()
    # The newest untriaged topics: a backfill running at the same time works oldest first.
    two = sorted([i for i in sorted(cache["ideas"].values(), key=lambda i: i["created"], reverse=True)
                  if i["created"] >= "2024-01-01" and i["id"] not in done][:2], key=lambda i: i["created"])
    if len(two) < 2:
        pytest.skip("every cached topic is triaged")
    ids = [i["id"] for i in two]
    path = cache_file(tmp_path, two, {i["id"]: cache["topics"].get(str(i["id"]), {}).get("text", "") for i in two})
    count = lambda: graph().ro_query("MATCH (t:Triage) WHERE t.topic IN $ids RETURN count(t)", {"ids": ids}).result_set[0][0]
    files = lambda: {f for f in triage.OUTBOX.rglob("*") if f.stem in {str(i) for i in ids}}
    n, before = count(), files()
    out = triage.backfill("2024-01-01", dry_run=True, path=path)
    assert [d["topic"]["id"] for d in out] == ids
    assert all(d["mode"] == "backfill" and d["steps"][0]["tool"] == "already_decided" and d["action"] in triage.ACTIONS
               for d in out)
    assert count() == n == 0 and files() == before   # a dry run writes nothing


def test_api_triage_filters(fake_topic):
    from fastapi.testclient import TestClient
    from lineage import server
    c = TestClient(server.app)
    for bad in ({"mode": "nope"}, {"action": "post_it"}):
        assert c.get("/api/triage", params=bad).status_code == 422
    triage.record({**fake_decision(fake_topic), "mode": "backfill"})
    rows = c.get("/api/triage", params={"mode": "backfill", "action": "reply_with_ruling", "limit": 100}).json()["rows"]
    assert FAKE in [r["topic"] for r in rows] and all(r["mode"] == "backfill" for r in rows)
    row = next(r for r in rows if r["topic"] == FAKE)
    assert row["wrote"][0]["rel"] == "SAME_IDEA_AS" and row["steps"][0]["tool"] == "already_decided"
    live = c.get("/api/triage", params={"mode": "live", "limit": 100}).json()["rows"]
    assert FAKE not in [r["topic"] for r in live]
    assert all(r["action"] == "route_to_experts"
               for r in c.get("/api/triage", params={"action": "route_to_experts"}).json()["rows"])


# ---------------------------------------------------------------- policy: cites, rejected ideas, status then

def test_a_post_naming_an_adopted_pep_builds_on_it(monkeypatch):
    fake_tools(monkeypatch, [pep_row(702, "Final", score=7.5), thread_row(14, match="related", committers=["Core One"])])
    d = triage.decide(topic("More deprecation decorators", tid=-20, text="PEP 702 added @deprecated; extend it to parameters."))
    assert d["action"] == "flag_for_review" and d["draft"] is None
    assert d["reason"].startswith("The post builds on PEP 702") and "Its authors should see it" in d["reason"]
    assert [r["why"] for r in d["reviewers"][:2]] == ["author of PEP 702, which the post builds on; posted on the forum in 2026"] * 2
    assert {e["pep"]: e["match"] for e in d["evidence"] if e["pep"]} == {702: "builds_on"}
    assert "draft_reply" not in tools_of(d)


def test_cites_reads_numbers_links_and_long_titles_in_the_post():
    pep = {"pep": 616, "title": "String methods to remove prefixes and suffixes"}
    assert triage.cites({"title": "Extend PEP-616"}, pep) and triage.cites({"title": "x", "text": "see pep-0616/"}, pep)
    assert triage.cites({"title": "x", "text": "As in string methods to remove  prefixes and suffixes, ..."}, pep)
    assert not triage.cites({"title": "x", "text": "PEP 6160 and PEP 61"}, pep)
    # A short title in the topic's own title names the idea, not the PEP.
    assert not triage.cites({"title": "Assignment expressions"}, {"pep": 572, "title": "Assignment Expressions"})


def test_point_to_existing_needs_a_comfortable_score(monkeypatch):
    fake_tools(monkeypatch, [pep_row(562, "Final", score=5.8)])
    d = triage.decide(topic("Module-level __getattribute__", tid=-21))
    assert d["action"] == "flag_for_review" and f"below the {triage.POINT_MIN}" in d["reason"]
    assert d["evidence"][0]["match"] == "related" and "is_current" not in tools_of(d)


def test_a_promotion_by_the_topics_own_thread_means_it_cites_the_pep(monkeypatch):
    fake_tools(monkeypatch, [pep_row(616, "Final", score=2.4, via_thread=-22)])   # -22 is the topic itself
    d = triage.decide(topic("tuple argument for str.removeprefix", tid=-22))
    assert d["action"] == "flag_for_review" and "the post cites it" in d["steps"][0]["outcome"]
    assert d["reason"].startswith("The post builds on PEP 616")
    fake_tools(monkeypatch, [pep_row(505, "Deferred", score=2.0, via_thread=-29)])   # citing a ruled PEP: a re-proposal
    assert triage.decide(topic("None-coalescing", tid=-29))["action"] == "reply_with_ruling"


def test_a_same_idea_rejected_idea_beats_the_adopted_pep(monkeypatch):
    rejected = {**pep_row(616, "Final"), "kind": "rejected_idea", "idea": "Accepting a tuple of affixes",
                "idea_url": "https://peps.python.org/pep-0616/#accepting-a-tuple-of-affixes", "idea_quote": "Not now.",
                "earlier_forms": [], "verdict": "set aside"}
    fake_tools(monkeypatch, [pep_row(616, "Final", score=7.0), rejected])   # already_decided returned both
    d = triage.decide(topic("removeprefix with a tuple", tid=-23))
    assert d["action"] == "reply_with_ruling" and "Accepting a tuple of affixes" in d["draft"]
    alts[616] = {**rejected, "score": 6.2}   # or the scan of the adopted PEP's own rejected ideas finds it
    fake_tools(monkeypatch, [pep_row(616, "Final", score=7.0)])
    d = triage.decide(topic("removeprefix with a tuple", tid=-24))
    assert d["action"] == "reply_with_ruling" and tools_of(d) == ["already_decided", "rejected_ideas", "draft_reply"]
    assert 'considered "Accepting a tuple of affixes" and set it aside' in d["reason"]
    assert "considered \"Accepting a tuple of affixes\" and set it aside" in d["draft"]
    assert {(e["kind"], e["match"]) for e in d["evidence"]} == {("rejected_idea", "same"), ("pep", "related")}


def test_a_pep_decided_after_the_topic_was_open_then(monkeypatch):
    decided_on[810] = "2025-11-03T16:25:04Z"
    fake_tools(monkeypatch, [pep_row(810, "Final", score=7.0, created="2025-10-02")])
    d = triage.decide(topic("Explicit lazy imports", tid=-25, created="2025-10-20"))
    assert d["action"] == "link_prior_threads" and "status_then" in tools_of(d)
    assert "already proposed this when the topic was posted" in d["reason"] and "Final only on 2025-11-03" in d["reason"]
    assert d["draft"].startswith("An open PEP already proposes this: PEP 810")
    assert "adopted" not in d["draft"] and "The ruling:" not in d["draft"] and "still open" in d["draft"]
    decided_on[315] = "2026-01-01"   # a ruling after the topic is not quoted either
    fake_tools(monkeypatch, [pep_row(315, "Rejected")])
    d = triage.decide(topic("do-while", tid=-26, created="2025-06-01"))
    assert d["action"] == "link_prior_threads" and "rejected" not in d["draft"]
    decided_on[315] = "2004-01-01"   # decided before the topic: today's status stands
    assert triage.decide(topic("do-while", tid=-27, created="2025-06-01"))["action"] == "reply_with_ruling"


def test_a_timed_out_tool_is_a_step_not_a_failure(monkeypatch):
    from redis.exceptions import ResponseError

    def slow(a):
        raise ResponseError("Query timed out")
    fake_tools(monkeypatch, [pep_row(3120, "Final", match="related", authors=[]),
                             thread_row(13, match="related", committers=["Dev A"])], connection=slow)
    d = triage.decide(topic("x", tid=-28))
    assert d["steps"][1] == {"tool": "connection", "args": {"a": "Dev A", "b": "PEP 3120"},
                             "why": d["steps"][1]["why"], "outcome": "timed out: kept, unranked"}
    assert d["reviewers"][0]["name"] == "Dev A"


# ---------------------------------------------------------------- routing only to active people

def test_inactive_people_are_not_routed(monkeypatch):
    fake_tools(monkeypatch, [pep_row(315, "Rejected")])
    inactive.add("Bob Author")
    d = triage.decide(topic("do-while", tid=-30))
    assert [r["name"] for r in d["reviewers"]] == ["Ann Author"] and d["inactive"] == ["Bob Author"]
    assert d["reviewers"][0]["why"] == "author of PEP 315; posted on the forum in 2026"
    step = next(s for s in d["steps"] if s["tool"] == "active_reviewers")
    assert step["outcome"] == "kept 1; dropped as inactive: Bob Author" and "3 years" in step["why"]
    inactive.add("Ann Author")
    d = triage.decide(topic("do-while", tid=-31))
    assert d["reviewers"] == [] and "Nobody active found; a moderator should pick reviewers." in d["reason"]


def test_last_active_reads_the_graph_window():
    # Guido posts on the forum every year; the authors of PEP 315 (2003) did nothing in 2004-2007 by this record.
    got = triage.last_active(["Guido van Rossum", "W Isaac Carroll", "No Such Person"], "2026-10-18")
    assert set(got) == {"Guido van Rossum"} and got["Guido van Rossum"][0] >= "2023-10-18"
    assert "W Isaac Carroll" not in triage.last_active(["W Isaac Carroll"], "2026-10-18")
