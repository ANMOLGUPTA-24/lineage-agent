"""Behaviour tests against the loaded graph (run `make ingest` first). These pin the four
answer outcomes and the guard, which are the parts that keep the agent honest."""
import uuid

import pytest

from lineage import agent, tools
from lineage.db import connect, resolve


@pytest.fixture(scope="module", autouse=True)
def graph_loaded():
    if resolve("python") not in connect().list_graphs():
        pytest.skip("graph not loaded")


def ids(path):
    return [x["id"] for x in path if "id" in x]


def test_unknown_pep_runs_no_lineage_query():
    r = tools.call("why", {"pep": "9999"})
    assert r["outcome"] == "unknown_entity"
    assert not any("REQUIRES|REFERENCES" in c for c in r["cypher"])


def test_nickname_resolves_through_fulltext():
    rows = tools.call("resolve", {"text": "walrus operator"})["rows"]
    assert rows[0]["number"] == 572 and rows[0]["confident"]


def test_ambiguous_name_is_not_confident():
    rows = tools.call("resolve", {"text": "pattern matching"})["rows"]
    assert len(rows) > 1 and not any(r["confident"] for r in rows)


def test_depth_reaches_further():
    shallow = tools.call("why", {"pep": "695", "depth": 1})
    deep = tools.call("why", {"pep": "695", "depth": 4})
    longest = lambda r: max(len(ids(p)) for p in r["paths"])
    assert longest(deep) > longest(shallow)
    assert all(len([e for e in p if "edge" in e]) <= 4 for p in deep["paths"])


def test_lineage_skips_process_peps():
    deep = tools.call("why", {"pep": "634", "depth": 4})
    for p in deep["paths"]:
        assert not {"PEP 1", "PEP 8", "PEP 13"} & set(ids(p)[1:])


def test_superseded_chain():
    r = tools.call("is_current", {"pep": "333"})
    assert r["rows"][0]["current"] is False
    assert r["rows"][0]["latest"][0]["pep"] == 3333


def test_inferred_decision_is_flagged():
    d = tools.call("why", {"pep": "572"})["rows"][0]["decision"]
    assert d["decided_by"] == [{"by": "Guido van Rossum", "inferred": True}]


def test_connection_returns_real_edges():
    r = tools.call("connection", {"a": "572", "b": "634"})
    assert r["outcome"] == "answered"
    assert ids(r["paths"][0])[0] == "PEP 572" and ids(r["paths"][0])[-1] == "PEP 634"


def test_connection_unknown_person():
    assert tools.call("connection", {"a": "572", "b": "Nobody Atall"})["outcome"] == "unknown_entity"


def test_memory_roundtrip_is_tenant_isolated():
    session = uuid.uuid4().hex[:6]
    try:
        tools.call("remember", {"pep": "572", "text": f"test finding {session}", "session": session})
        got = tools.call("recall", {"pep": "572"})
        assert got["outcome"] == "memory" and any(session in r["text"] for r in got["rows"])
        other = tools.call("recall", {"pep": "572"}, tenant="python_other_tenant")
        assert other["outcome"] == "unknown_entity"  # a separate graph knows nothing
    finally:
        tools.graph().query("MATCH (f:Finding) WHERE f.session = $s DETACH DELETE f", {"s": session})


def test_guard_rejects_invented_pep():
    steps = [tools.call("is_current", {"pep": "333"})]
    assert agent.guard("PEP 333 was replaced by PEP 3333.", steps) == []
    assert agent.guard("PEP 333 was replaced by PEP 9000.", steps) == ["PEP 9000"]


def test_router_without_llm(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    out = agent.ask("Is PEP 333 still current?")
    assert out["mode"] == "router" and "3333" in out["answer"]


class FakeLLM:
    """Scripted OpenAI-compatible client: one tool call, then a final answer."""

    def __init__(self, final: str):
        from types import SimpleNamespace as NS
        call = NS(id="c1", function=NS(name="is_current", arguments='{"pep": "333"}'),
                  model_dump=lambda: {"id": "c1", "type": "function",
                                      "function": {"name": "is_current", "arguments": '{"pep": "333"}'}})
        self.replies = iter([NS(content=None, tool_calls=[call]), NS(content=final, tool_calls=None)])
        self.chat = NS(completions=NS(create=lambda **_: NS(choices=[NS(message=next(self.replies))])))


def test_llm_answer_passes_guard(monkeypatch):
    monkeypatch.setattr(agent, "llm_client", lambda: (FakeLLM("PEP 333 was superseded by PEP 3333."), "fake"))
    out = agent.ask("Is PEP 333 still current?")
    assert out["mode"] == "llm" and out["answer"] == "PEP 333 was superseded by PEP 3333."


def test_llm_hallucination_is_replaced(monkeypatch):
    monkeypatch.setattr(agent, "llm_client", lambda: (FakeLLM("PEP 333 was superseded by PEP 9000."), "fake"))
    out = agent.ask("Is PEP 333 still current?")
    assert out["mode"] == "guarded" and out["rejected"]["unsupported"] == ["PEP 9000"]
    assert "3333" in out["answer"] and "9000" not in out["answer"]


def test_never_adopted_ancestors_are_not_foundations():
    r = tools.call("why", {"pep": "572"})["rows"][0]
    assert "PEP 3150" not in r["builds_on"]
    assert {"pep": "PEP 3150", "status": "Deferred"} in r["earlier_proposals"]


def test_rejected_approach_is_quoted_with_a_deep_link():
    r = tools.call("why", {"pep": "572"})["rows"][0]
    x = next(a for a in r["rejected_approaches"] if a["pep"] == 3150)
    assert "PEP 3150" in x["quote"] and x["url"].startswith("https://peps.python.org/pep-0572/#")


def test_rejects_approach_only_points_at_unadopted_older_peps():
    bad = tools.graph().ro_query("""MATCH (p:PEP)-[:REJECTS_APPROACH_OF]->(q:PEP)
                                    WHERE q.type = 'Process' OR q.status IN ['Final', 'Accepted', 'Active']
                                       OR q.created >= p.created
                                    RETURN count(*)""").result_set[0][0]
    assert bad == 0


# ---------------------------------------------------------------- already decided?

def test_rejected_idea_comes_with_its_ruling_text():
    r = tools.call("already_decided", {"idea": "add a do-while loop"})
    top = r["rows"][0]
    assert top["pep"] == 315 and top["match"] == "same" and top["verdict"] == "rejected"
    assert top["reason"]["url"] == "https://peps.python.org/pep-0315/#notice"


def test_recurrence_follows_rejects_approach_edges():
    top = tools.call("already_decided", {"idea": "statement-local name bindings with a given clause"})["rows"][0]
    assert top["pep"] == 3150
    assert {572, 403} <= {b["pep"] for b in top["came_back"]}


def test_unrecorded_idea_is_not_called_new():
    r = tools.call("already_decided", {"idea": "add a banana keyword"})
    assert r["outcome"] == "not_in_graph" and r["rows"] == []
    assert "not proof" in r["note"]


def test_loose_match_is_labelled_related():
    r = tools.call("already_decided", {"idea": "make type hints mandatory and enforced at runtime"})
    assert r["rows"] and all(x["match"] == "related" for x in r["rows"])


def test_draft_reply_only_links_what_the_graph_returned():
    r = tools.call("draft_reply", {"idea": "add a switch statement"})
    draft = r["rows"][0]["draft"]
    assert "PEP 3103" in draft and "#rejection-notice" in draft
    import re
    urls = set(re.findall(r"https://\S+", draft))
    row_text = str(r["rows"][1:])
    assert urls and all(u in row_text for u in urls)


def test_trace_has_params_but_not_raw_vectors():
    r = tools.call("already_decided", {"idea": "add a switch statement"})
    assert len(r["params"]) == len(r["cypher"])
    assert any(p.get("v") == "<vector[384]>" for p in r["params"])


def test_guard_rejects_invented_quote():
    steps = [tools.call("already_decided", {"idea": "add a switch statement"})]
    real = 'Guido wrote: "A quick poll during my keynote presentation at PyCon 2007 shows this proposal has no popular support."'
    fake = 'Guido wrote: "Switch statements are banned because they are slower than dicts."'
    assert agent.unsupported_quotes(real, steps) == []
    assert agent.unsupported_quotes(fake, steps) != []


def test_router_sends_proposals_to_already_decided(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    out = agent.ask("Has anyone proposed a do-while loop?")
    assert out["steps"][-1]["tool"] == "already_decided" and "PEP 315" in out["answer"]


@pytest.mark.parametrize("question", ["Has anyone proposed a do-while loop?", "Why doesn't Python have a do-while loop?",
                                      "Draft a reply to someone proposing a do-while loop"])
def test_question_framing_does_not_weaken_the_match(question):
    top = tools.call("already_decided", {"idea": question})["rows"][0]
    assert top["pep"] == 315 and top["match"] == "same"


# ---------------------------------------------------------------- expertise (PageRank / WCC projections)

def test_who_knows_ranks_by_topic_pagerank_with_evidence_paths():
    r = tools.call("who_knows", {"subject": "typing"})
    row = r["rows"][0]
    assert row["method"] == "PageRank over INFLUENCE_typing"
    shares = [p["influence_share"] for p in row["most_built_on"]]
    assert shares == sorted(shares, reverse=True)
    # The most active person is reported even when PageRank ranks them lower.
    assert row["most_active"][0]["items"] >= max(p["items"] for p in row["most_built_on"])
    assert r["paths"] and all(p[0]["label"] == "Person" and p[-1]["label"] == "Person" for p in r["paths"])


def test_who_knows_for_a_pep_lists_its_own_authors_first():
    row = tools.call("who_knows", {"subject": "572"})["rows"][0]
    assert {"name": "Chris Angelico", "role": "authored"} in row["direct"]


def test_group_author_strings_are_not_ranked_as_people():
    row = tools.call("who_knows", {"subject": "governance", "limit": 20})["rows"][0]
    assert not any("community" in p["name"].lower() for p in row["most_built_on"])


def test_bus_factor_reports_wcc_split():
    c = tools.call("bus_factor", {"subject": "3115"})["rows"][0]["connected"]
    assert c["largest_without_them"] < c["largest_group"] and c["cut_off_if_they_leave"] >= 1


def test_projection_has_one_edge_per_pair():
    dup = tools.graph().ro_query("""MATCH (a:Person)-[e:INFLUENCE_typing]->(b:Person)
                                    WITH a, b, count(e) AS n WHERE n > 1 RETURN count(*)""").result_set[0][0]
    assert dup == 0


def test_connection_prefers_a_real_citation_over_hubs():
    # Before hub-aware person/topic weights this was "484 <- Guido -> 695", then "both are typing PEPs".
    p = tools.call("connection", {"a": "484", "b": "695"})["paths"][0]
    assert [x for x in p if "edge" in x] == [{"edge": "REFERENCES", "forward": False}]


def test_connection_uses_rejected_approach_edges():
    p = tools.call("connection", {"a": "3150", "b": "572"})["paths"][0]
    assert any(x.get("edge") == "REJECTS_APPROACH_OF" for x in p)


def test_why_quotes_rationale_and_ruling_notice():
    r = tools.call("why", {"pep": "3103"})["rows"][0]
    assert r["rationale"]["url"].endswith("#rationale")
    assert r["ruling_notice"]["quote"].startswith("A quick poll during my keynote")


def test_guard_catches_a_paraphrased_rationale():
    steps = [tools.call("why", {"pep": "572"})]
    real = steps[0]["rows"][0]["rationale"]["quote"]
    edited = real.replace("important part of programming", "essential part of programming")
    assert agent.unsupported_quotes(f'The PEP says "{real}"', steps) == []
    assert agent.unsupported_quotes(f'The PEP says "{edited}"', steps) != []


def test_router_sends_whats_new_to_changes(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert agent.ask("What changed recently?")["steps"][-1]["tool"] == "changes"


def test_idea_without_a_pep_points_to_its_forum_thread():
    r = tools.call("already_decided", {"idea": "multi-line lambdas"})
    threads = [x for x in r["rows"] if x["kind"] == "thread"]
    if not threads:
        pytest.skip("graph built without discuss.python.org threads")
    assert any("lambda" in x["title"].lower() for x in threads)
    assert not [x for x in r["rows"] if x["kind"] != "thread" and x["match"] == "same"]
    text = agent.template(r)
    assert "discuss.python.org" in text and threads[0]["url"] in text


# ---------------------------------------------------------------- session memory

@pytest.fixture
def session():
    sid = "t" + uuid.uuid4().hex[:10]
    yield sid
    tools.graph().query("MATCH (s:Session {id: $s}) OPTIONAL MATCH (s)-[:ASKED]->(q) DETACH DELETE q, s", {"s": sid})


def test_session_links_new_answer_to_earlier_pep(monkeypatch, session):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    agent.ask("Why does Python have the walrus operator?", session=session)
    out = agent.ask("Has anyone proposed statement-local name bindings with a given clause?", session=session)
    link = out["steps"][-1]
    assert link["tool"] == "session_link" and link["rows"][0]["earlier"] == 572
    assert "Earlier in this session you asked about PEP 572" in out["answer"]


def test_my_session_reads_history_from_the_graph(monkeypatch, session):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    agent.ask("Is PEP 333 still current?", session=session)
    out = agent.ask("What did I ask earlier?", session=session)
    assert out["steps"][0]["tool"] == "my_session" and "PEP 333" in out["answer"]


def test_sessions_are_isolated(monkeypatch, session):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    agent.ask("Is PEP 333 still current?", session=session)
    assert tools.my_session("someone_else_entirely")["outcome"] == "not_in_graph"
    assert tools.my_session("bad id with spaces")["outcome"] == "not_in_graph"


def test_who_knows_for_a_pep_outside_every_area():
    # PEP 315 is in no topic and no large community: the local fallback must still answer.
    r = tools.call("who_knows", {"subject": "315"})
    assert r["outcome"] == "answered" and r["rows"][0]["people"]


def test_search_indexes_exist():
    # Every index already_decided searches; a missing one fails silently at query time, so check here.
    rows = tools.graph().ro_query("CALL db.indexes() YIELD label, types RETURN label, types").result_set
    have = {(label, t) for label, types in rows for field, ts in types.items() for t in ts}
    for need in [("PEP", "VECTOR"), ("PEP", "FULLTEXT"), ("Alternative", "VECTOR"), ("Alternative", "FULLTEXT"),
                 ("PEP", "RANGE"), ("Alternative", "RANGE"), ("Person", "RANGE"), ("Session", "RANGE")]:
        assert need in have, need
    if tools.graph().ro_query("MATCH (t:Thread) RETURN count(t)").result_set[0][0]:
        assert ("Thread", "VECTOR") in have and ("Thread", "FULLTEXT") in have


# ---------------------------------------------------------------- advisor round 3 regressions

@pytest.mark.parametrize("question,idea", [
    ("Did anyone ever suggest multi-line lambdas?", "multi-line lambdas"),
    ("Should Python add a switch statement?", "a switch statement"),
    ("Has anyone proposed allowing keyword arguments in indexing?", "allowing keyword arguments in indexing"),
    ("Why can't I write a lambda with several statements?", "write a lambda with several statements"),
])
def test_paraphrased_proposals_are_routed_and_unframed(question, idea):
    assert tools.idea_text(question) == idea
    assert agent.route(question, "python")[-1]["tool"] == "already_decided"


def test_who_decided_resolves_the_feature_once():
    steps = agent.route("Who decided the walrus operator?", "python")
    assert [s["tool"] for s in steps].count("resolve") == 1
    assert steps[-1]["tool"] == "why" and steps[-1]["rows"][0]["pep"] == 572


def test_came_back_follows_chains_and_quotes_the_citing_idea():
    row, _ = tools.pep_context(tools.Run("python"), 472)
    back = {b["pep"]: b for b in row["came_back"]}
    assert 637 in back and back[696]["through"] == 637 and back[696]["idea"] and back[696]["quote"]
    text = tools.came_back_text(back[696])
    assert "set it aside" not in text and "cites PEP 637" in text


def test_pep_named_by_a_same_idea_thread_is_promoted():
    rows = tools.call("already_decided", {"idea": "null coalescing"})["rows"]
    assert rows[0]["kind"] == "pep" and rows[0]["pep"] == 505 and rows[0]["match"] == "same"


def test_connection_same_pep_and_weak_links():
    assert tools.call("connection", {"a": "572", "b": "572"})["outcome"] == "not_in_graph"
    assert tools.call("connection", {"a": "484", "b": "695"})["rows"][0]["strength"] == "direct"


def test_unclear_resolve_is_worded_as_a_guess():
    step = tools.call("resolve", {"text": "zzqx flurb"})
    if step["rows"] and not any(r["confident"] for r in step["rows"]):
        assert agent.template(step).startswith("I couldn't tell")


@pytest.mark.parametrize("answer,flag", [
    ("PEP 572 was rejected by Elon Musk in 2010.", "Elon Musk"),
    ("PEP 572 was rejected by Elon Musk in 2010.", "PEP 572 called rejected (recorded: Final)"),
    ("PEP 572 was rejected by Elon Musk in 2010.", "year 2010"),
    ("PEP 572 was accepted by Guido Musk.", "Guido Musk"),
])
def test_guard_catches_invented_people_verdicts_and_years(answer, flag):
    assert flag in agent.guard(answer, [tools.call("why", {"pep": "572"})])


def test_guard_passes_a_faithful_answer():
    steps = [tools.call("why", {"pep": "572"})]
    ok = ("PEP 572 (Assignment Expressions) is Final, written by Tim Peters, Guido van Rossum and Chris Angelico; "
          "it was accepted, attributed to Guido van Rossum. The Steering Council era came later. "
          "It set aside PEP 3150, which was deferred.")
    assert agent.guard(ok, steps) == []


@pytest.mark.parametrize("question", ["Is PEP -1 still current?", "Is PEP 99999999999999999999 still current?"])
def test_malformed_pep_numbers_are_unknown_not_guessed(question):
    step = agent.route(question, "python")[-1]
    assert step["outcome"] == "unknown_entity" and not step.get("rows")


def test_unknown_person_in_a_connection_fails_loudly():
    step = agent.route("How is Zaphod Beeblebrox connected to PEP 572?", "python")[-1]
    assert step["tool"] == "connection" and step["outcome"] == "unknown_entity"
    assert "Zaphod" in agent.template(step)


def test_person_to_pep_connection():
    step = agent.route("How is Guido van Rossum connected to PEP 572?", "python")[-1]
    assert step["tool"] == "connection" and step["outcome"] == "answered"


def test_session_memory_is_capped(monkeypatch, session):
    monkeypatch.setattr(tools, "SESSION_CAP", 2)
    for i in range(4):
        tools.record_turn(session, f"q{i}", "router", [8])
    n = tools.graph().ro_query("MATCH (:Session {id: $s})-[:ASKED]->(q) RETURN count(q)", {"s": session}).result_set[0][0]
    assert n == 2


def test_rate_limit():
    from lineage import server
    server._hits.clear()
    assert not any(server.throttled("t") for _ in range(server.RATE))
    assert server.throttled("t")


def test_draft_leads_with_what_python_has_now():
    draft = tools.call("draft_reply", {"idea": "add a switch statement"})["rows"][0]["draft"]
    top = draft.split("\n\n")[1]
    assert "PEP 635" in top and "Pattern Matching" in top and "since Python 3.10" in top and "PEP 3103" in top
    draft = tools.call("draft_reply", {"idea": "forbid return break continue breaking out of finally"})["rows"][0]["draft"]
    assert draft.split("\n\n")[1].startswith("What Python follows now is PEP 765")


def test_draft_without_a_ruling_says_so_instead_of_quoting_the_abstract():
    draft = tools.call("draft_reply", {"idea": "define a memory model for python concurrency"})["rows"][0]["draft"]
    assert "No ruling is recorded for it: it was withdrawn by its authors." in draft
    assert "From the PEP:" not in draft and "answer the reasons above" not in draft
    assert "start from where that discussion stopped" in draft
