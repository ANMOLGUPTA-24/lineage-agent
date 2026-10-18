"""Triage agent tests against the loaded graph (run `make ingest` first). Topics are synthetic (negative ids,
no network); the round trip writes one fake topic into the graph and the outbox and removes it again."""
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
    assert any(r["why"] == "author of PEP 315" for r in d["reviewers"])


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


EARLIER = 65193   # a real forum thread ("New function `repr_args` in `pprint` or `reprlib`")


def fake_decision(t):
    return {"topic": t, "action": "reply_with_ruling", "reason": "test reason", "confidence": 7.5,
            "evidence": [{"kind": "pep", "pep": 315, "thread_id": None, "title": "Enhanced While Loop", "match": "same",
                          "url": "https://peps.python.org/pep-0315/"},
                         {"kind": "thread", "pep": None, "thread_id": EARLIER, "title": "x", "match": "related", "url": "u"}],
            "reviewers": [{"name": "Raymond Hettinger", "why": "author of PEP 315"}], "draft": "drafted text", "paths": []}


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
    finally:
        dst.delete()


def test_unreachable_webhook_does_not_stop_the_record(fake_topic, monkeypatch, capsys):
    monkeypatch.setenv("TRIAGE_WEBHOOK_URL", "http://127.0.0.1:9/")
    d = {"topic": fake_topic, "action": "route_to_experts", "reason": "r", "confidence": None, "evidence": [],
         "reviewers": [], "draft": None, "paths": []}
    assert triage.record(d) and triage.already_triaged(FAKE)
    assert (triage.OUTBOX / f"{FAKE}.md").exists() and "webhook failed" in capsys.readouterr().out
