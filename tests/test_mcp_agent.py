"""The MCP client agent: starts the Lineage MCP server over stdio and chooses tools from its catalogue."""
import asyncio
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp.client.stdio")
from mcp import Client  # noqa: E402

spec = importlib.util.spec_from_file_location("mcp_agent", Path(__file__).resolve().parents[1] / "scripts" / "mcp_agent.py")
mcp_agent = importlib.util.module_from_spec(spec)
sys.modules["mcp_agent"] = mcp_agent  # dataclasses look the module up
spec.loader.exec_module(mcp_agent)


def test_tool_list_and_policy_run():
    out = io.StringIO()
    res = asyncio.run(mcp_agent.run({"idea": "add a do-while loop"}, record=False, max_steps=4, use_llm=False, out=out))
    assert {"already_decided", "triage_topic", "triage_queue"} <= set(res["tools"])
    steps = res["trace"]
    assert steps[0]["tool"] == "already_decided"
    assert steps[1]["tool"] in ("is_current", "why") and steps[1]["args"]["pep"] == "315"
    assert "PEP 315" in steps[1]["why"]  # the follow-up was chosen because of the first result
    assert "PEP 315" in res["decision"]


class FakeLLM:
    """Stands in for an OpenAI-compatible client: calls already_decided once, then answers."""

    def __init__(self):
        self.seen_tools = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.calls = 0

    def create(self, model, messages, tools, **kw):
        self.seen_tools = [t["function"]["name"] for t in tools]
        self.calls += 1
        if self.calls == 1:
            call = SimpleNamespace(id="c1", function=SimpleNamespace(
                name="already_decided", arguments=json.dumps({"idea": "add a do-while loop"})))
            msg = SimpleNamespace(content="check prior rulings", tool_calls=[call])
        else:
            assert json.loads(messages[-1]["content"])["outcome"] == "answered"
            msg = SimpleNamespace(content="Rejected as PEP 315.", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_llm_path_uses_advertised_schemas():
    async def go():
        async with Client(mcp_agent.server_params()) as client:
            catalogue = {t.name: t for t in (await client.list_tools()).tools}
            llm = FakeLLM()
            res = await mcp_agent.run_llm(client, catalogue, {"idea": "add a do-while loop"}, False, 4,
                                          io.StringIO(), llm, "fake")
            return llm, res
    llm, res = asyncio.run(go())
    assert set(llm.seen_tools) >= {"already_decided", "triage_topic", "triage_queue"}
    assert [s["tool"] for s in res["trace"]] == ["already_decided"]
    assert res["decision"] == "Rejected as PEP 315."
