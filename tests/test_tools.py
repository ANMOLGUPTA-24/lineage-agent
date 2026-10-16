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
