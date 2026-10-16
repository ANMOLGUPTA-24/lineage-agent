"""Behaviour tests against the loaded graph (run `make ingest` first). These pin the four
answer outcomes and the guard, which are the parts that keep the agent honest."""
import uuid

import pytest

from lineage import agent, tools
from lineage.db import connect


@pytest.fixture(scope="module", autouse=True)
def graph_loaded():
    if "python" not in connect().list_graphs():
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
