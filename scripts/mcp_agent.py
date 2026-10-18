"""A real MCP client agent for Lineage: it starts the Lineage MCP server over stdio, reads the tool
catalogue the server advertises, and works through a task by choosing tools from that catalogue
based on what each result says.

  uv run python scripts/mcp_agent.py --idea "add a do-while loop"
  uv run python scripts/mcp_agent.py --topic 108762            # dry run: record=False
  uv run python scripts/mcp_agent.py --topic 108762 --record   # also write the decision to the graph

Who chooses the next tool:
  * LLM_API_KEY set (same OpenAI-compatible convention as lineage/agent.py: LLM_API_KEY, LLM_BASE_URL,
    LLM_MODEL) -> the model chooses, by function calling over the schemas returned by tools/list.
  * otherwise -> a small policy (POLICY below) maps each structured result (outcome, rows, paths) to
    candidate next calls. Only tools the server actually advertises are ever called, and the
    arguments are checked against the advertised input schema.

triage_topic is always called with record=False unless --record is passed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
ENV_PREFIXES = ("FALKORDB_", "LINEAGE_", "FETCH", "TRIAGE_")
NOISE = ("cypher", "params", "explain", "ms")  # query internals the chooser doesn't need


def server_params() -> StdioServerParameters:
    env = {k: v for k, v in os.environ.items() if k.startswith(ENV_PREFIXES)}
    return StdioServerParameters(command=sys.executable, args=["-m", "lineage.mcp_server"], cwd=str(ROOT), env=env)


def parse_result(r) -> dict:
    if r.structured_content:
        d = r.structured_content
        return d.get("result", d) if isinstance(d, dict) else {"rows": d}
    text = "".join(getattr(c, "text", "") for c in r.content)
    try:
        d = json.loads(text)
    except ValueError:
        d = {"note": text}
    if r.is_error:
        d = {"outcome": "error", "note": text[:300]}
    return d if isinstance(d, dict) else {"rows": d}


# ------------------------------------------------------------------ one-line outcome per tool

def pep_label(row: dict) -> str:
    return f"PEP {row['pep']} '{row.get('title', '')}' ({row.get('status', '?')})"


def summarize(tool: str, d: dict) -> str:
    rows = d.get("rows") or []
    if tool == "triage_topic":
        if "action" not in d:
            return f"outcome={d.get('outcome')}: {d.get('note', '')}"
        rec = f", triage_id={d['triage_id']}" if d.get("triage_id") else ""
        return f"action={d['action']} (confidence {d.get('confidence')}{rec}): {d.get('reason', '')}"
    head = f"outcome={d.get('outcome', '?')}"
    if tool == "draft_reply" and rows and rows[0].get("draft"):
        cites = sorted({f"PEP {r['pep']}" for r in rows if r.get("pep")})
        return f"{head}; drafted a {len(rows[0]['draft'])}-char reply citing {', '.join(cites) or 'no PEP'}"
    if tool in ("already_decided", "draft_reply"):
        hits = [r for r in rows if r.get("pep")]
        if hits:
            r = hits[0]
            return f"{head}; best match {pep_label(r)}, match={r.get('match')}, verdict={r.get('verdict')}"
    if tool == "who_knows" and rows:
        return f"{head}; " + ", ".join(p["name"] for p in rows[0].get("people", [])[:5])
    if tool == "connection" and rows:
        r = rows[0]
        return f"{head}; {r.get('hops')} hop(s), strength={r.get('strength')}"
    if tool == "resolve" and rows:
        return f"{head}; " + ", ".join(f"PEP {r['number']}" + ("" if r.get("confident") else "?")
                                       for r in rows[:5] if r.get("number"))
    if tool == "why" and rows:
        r = rows[0]
        ppl = ", ".join(p["name"] for p in r.get("people", [])[:3])
        ruling = (r.get("decision") or r.get("ruling_notice") or {}).get("quote", "")
        return f"{head}; {pep_label(r)} by {ppl}" + (f"; ruling: \"{ruling[:90]}...\"" if ruling else "")
    note = d.get("note") or ""
    return f"{head}; {len(rows)} row(s), {len(d.get('paths') or [])} path(s)" + (f"; {note}" if note else "")


# ------------------------------------------------------------------ policy (no LLM)

@dataclass
class State:
    task: dict
    record: bool
    peps: dict = field(default_factory=dict)     # pep number -> label, in the order found
    people: list = field(default_factory=list)
    decision: dict = field(default_factory=dict)


def first_calls(st: State) -> list[tuple[str, dict, str]]:
    if "topic" in st.task:
        return [("triage_topic", {"topic_id": st.task["topic"], "record": False},
                 "task is a discuss.python.org topic; triage_topic (dry run) reads it and the graph's evidence")]
    return [("already_decided", {"idea": st.task["idea"]},
             "task is an idea; first check whether Python already decided it")]


def follow_ups(st: State, tool: str, args: dict, d: dict) -> list[tuple[str, dict, str]]:
    """Map one structured result to candidate next calls, each with the reason that led to it."""
    out, rows = [], d.get("rows") or []

    def pep_found(n, why_):
        if n and n not in st.peps:
            st.peps[n] = why_
            out.append(("is_current", {"pep": str(n)}, f"{why_}; is it still how Python works?"))
            out.append(("why", {"pep": str(n)}, f"{why_}; get the ruling and who made it"))

    if tool == "already_decided":
        hits = [r for r in rows if r.get("pep")]
        if d.get("outcome") == "answered" and hits:
            top = hits[0]
            st.decision.update(kind="already_decided", pep=top["pep"], label=pep_label(top), verdict=top.get("verdict"))
            pep_found(top["pep"], f"already_decided matched {pep_label(top)} (match={top.get('match')})")
            for c in top.get("came_back") or []:
                pep_found(c["pep"], f"PEP {top['pep']}'s row lists later PEP {c['pep']} that took the idea up again")
        elif rows:  # thread / rejected-idea matches without a PEP
            st.decision.update(kind="prior_discussion", label=rows[0].get("title", "a prior thread"))
            out.append(("resolve", {"text": st.task["idea"]}, "match was a thread, not a PEP; look for related PEPs"))
        else:
            st.decision.update(kind="new")
            out.append(("resolve", {"text": st.task["idea"]},
                        f"outcome={d.get('outcome')}: nothing matched closely; look for nearby PEPs"))
            out.append(("who_knows", {"subject": st.task["idea"]}, "no prior ruling; find people who know the area"))
    elif tool == "triage_topic" and "action" in d:
        st.decision.update(kind="triage", action=d["action"], reason=d.get("reason"), draft=d.get("draft"))
        for e in d.get("evidence") or []:
            if e.get("pep"):
                pep_found(e["pep"], f"triage evidence cites PEP {e['pep']} (match={e.get('match')})")
        if not any(e.get("pep") for e in d.get("evidence") or []):
            title = (d.get("topic") or {}).get("title", "")
            if title:
                out.append(("resolve", {"text": title}, "triage cited no PEP; resolve the topic title to PEPs"))
        for p in d.get("reviewers") or []:
            if p["name"] not in st.people:
                st.people.append(p["name"])
                if st.peps:
                    n = next(iter(st.peps))
                    out.append(("connection", {"a": p["name"], "b": f"PEP {n}"},
                                f"triage suggests {p['name']} ({p.get('why')}); check how they connect to PEP {n}"))
    elif tool == "draft_reply" and rows and rows[0].get("draft"):
        st.decision["draft"] = rows[0]["draft"]
    elif tool == "resolve":
        for r in rows[:2]:
            if r.get("number") and r.get("confident"):
                pep_found(r["number"], f"resolve found PEP {r['number']} '{r.get('title')}' (confident)")
    elif tool == "why" and rows:
        n = rows[0].get("pep")
        names = [p["name"] for p in rows[0].get("people", [])]
        for later in rows[0].get("superseded_by") or []:
            num = later.get("pep") if isinstance(later, dict) else later
            pep_found(num, f"PEP {n} was superseded by PEP {num}")
        if names and n == next(iter(st.peps), None):
            out.append(("who_knows", {"subject": f"PEP {n}"},
                        f"why lists {', '.join(names[:2])} as PEP {n}'s people; who else knows this area?"))
    elif tool == "is_current" and rows:
        for later in rows[0].get("latest") or []:
            num = later.get("pep") if isinstance(later, dict) else later
            pep_found(num, f"is_current says PEP {rows[0]['pep']} is replaced by PEP {num}")
        st.decision.setdefault("current", {})[rows[0]["pep"]] = rows[0].get("current")
    elif tool == "who_knows" and rows:
        people = [p["name"] for p in rows[0].get("people", [])]
        new = [p for p in people if p not in st.people]
        st.people.extend(new)
        if new and st.peps and not any(c == "connection" for c, _, _ in out):
            n = next(iter(st.peps))
            out.append(("connection", {"a": new[0], "b": f"PEP {n}"},
                        f"who_knows ranks {new[0]} first; confirm the graph path from them to PEP {n}"))
    return out


def closing_calls(st: State) -> list[tuple[str, dict, str]]:
    if "topic" in st.task:
        if st.record:
            return [("triage_topic", {"topic_id": st.task["topic"], "record": True},
                     "--record passed: write the decision to the graph and the outbox")]
        return []
    if st.decision.get("kind") in ("already_decided", "prior_discussion"):
        return [("draft_reply", {"idea": st.task["idea"]}, "a prior ruling exists; draft the reply (nothing is posted)")]
    return []


def final_decision(st: State) -> str:
    d = st.decision
    who = f" People to route to: {', '.join(st.people[:3])}." if st.people else ""
    if d.get("kind") == "triage":
        rec = "recorded in the graph" if st.record else "dry run, not recorded (pass --record to write it)"
        return f"{d['action']}: {d.get('reason')}{who} [{rec}]"
    if d.get("kind") == "already_decided":
        cur = d.get("current", {}).get(d["pep"])
        state = "not part of Python" if cur is False else "current" if cur else "status unknown"
        later = [f"PEP {n}" for n in st.peps if n != d["pep"]]
        tail = f" Later attempts: {', '.join(later)}." if later else ""
        return f"Already decided: {d['label']}, verdict {d.get('verdict')} ({state}). Reply with the ruling.{tail}{who}"
    if d.get("kind") == "prior_discussion":
        return f"Discussed before ({d['label']}); link the prior thread.{who}"
    found = f" Nearby PEPs: {', '.join(f'PEP {n}' for n in st.peps)}." if st.peps else ""
    return f"No earlier ruling in the graph (not proof the idea is new).{found}{who}"


# ------------------------------------------------------------------ the agent loop

def valid(tool: str, args: dict, catalogue: dict) -> bool:
    t = catalogue.get(tool)
    if not t:
        return False
    schema = t.input_schema or {}
    return set(schema.get("required", [])) <= set(args) and set(args) <= set(schema.get("properties", {}))


async def run_policy(client, catalogue: dict, task: dict, record: bool, max_steps: int, out) -> dict:
    st = State(task=task, record=record)
    queue, done, trace = first_calls(st), set(), []
    closed = False
    while len(trace) < max_steps:
        if not queue:
            if closed:
                break
            queue, closed = closing_calls(st), True
            continue
        tool, args, reason = queue.pop(0)
        key = (tool, json.dumps(args, sort_keys=True))
        if key in done or not valid(tool, args, catalogue):
            continue
        done.add(key)
        d = parse_result(await client.call_tool(tool, args))
        step = {"tool": tool, "args": args, "why": reason, "outcome": summarize(tool, d)}
        trace.append(step)
        print_step(len(trace), step, out)
        queue.extend(follow_ups(st, tool, args, d))
    decision = final_decision(st)
    print(f"\nDecision: {decision}", file=out)
    if st.decision.get("draft"):
        print("\nDraft reply (not posted):\n  " + st.decision["draft"].replace("\n", "\n  "), file=out)
    return {"trace": trace, "decision": decision, "mode": "policy"}


SYSTEM = ("You triage Python language ideas using the Lineage MCP tools (a graph of PEPs, people, rulings and "
          "discuss.python.org threads). Choose one tool at a time based on the previous results: check whether the "
          "idea was already decided, follow up on any PEP that matched (is it current? why was it ruled that way?), "
          "find who knows the area, and end with a one-paragraph decision. Never call triage_topic with record=true "
          "unless the user said recording is allowed. Stop calling tools when you have enough evidence.")


def llm_client():
    key = os.getenv("LLM_API_KEY")
    if not key:
        return None, None
    from openai import OpenAI
    base = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    return OpenAI(api_key=key, base_url=base), os.getenv("LLM_MODEL", "gemini-2.5-flash")


def openai_tools(catalogue: dict) -> list[dict]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                              "parameters": t.input_schema}} for t in catalogue.values()]


async def run_llm(client, catalogue: dict, task: dict, record: bool, max_steps: int, out, llm, model) -> dict:
    ask = (f"discuss.python.org topic {task['topic']}" if "topic" in task else f"the idea: {task['idea']!r}")
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Triage {ask}. Recording is {'allowed' if record else 'NOT allowed'}."}]
    trace, decision = [], ""
    while True:
        resp = llm.chat.completions.create(model=model, messages=msgs, tools=openai_tools(catalogue),
                                           **({} if len(trace) < max_steps else {"tool_choice": "none"}))
        m = resp.choices[0].message
        if not m.tool_calls:
            decision = (m.content or "").strip()
            break
        msgs.append({"role": "assistant", "content": m.content or "",
                     "tool_calls": [{"id": c.id, "type": "function",
                                     "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                    for c in m.tool_calls]})
        for c in m.tool_calls:
            tool, args = c.function.name, json.loads(c.function.arguments or "{}")
            if tool == "triage_topic" and not record:
                args["record"] = False
            if tool not in catalogue:
                d = {"outcome": "error", "note": f"{tool} is not in the server's tool list"}
            else:
                d = parse_result(await client.call_tool(tool, args))
            step = {"tool": tool, "args": args, "why": (m.content or "model's choice").strip()[:160],
                    "outcome": summarize(tool, d)}
            trace.append(step)
            print_step(len(trace), step, out)
            slim = {k: v for k, v in d.items() if k not in NOISE}
            msgs.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(slim, default=str)[:6000]})
    print(f"\nDecision: {decision}", file=out)
    return {"trace": trace, "decision": decision, "mode": f"llm:{model}"}


def print_step(i: int, step: dict, out) -> None:
    args = ", ".join(f"{k}={v!r}" for k, v in step["args"].items())
    print(f"\n[{i}] {step['tool']}({args})\n    why: {step['why']}\n    ->  {step['outcome']}", file=out)


async def run(task: dict, record: bool = False, max_steps: int = 8, use_llm: bool = True, out=sys.stdout) -> dict:
    llm, model = llm_client() if use_llm else (None, None)
    async with Client(server_params()) as client:
        catalogue = {t.name: t for t in (await client.list_tools()).tools}
        print(f"Connected to MCP server '{client.server_info.name if client.server_info else 'lineage'}' over stdio; "
              f"it advertises {len(catalogue)} tools: {', '.join(catalogue)}", file=out)
        if llm:
            print(f"Chooser: {model} via function calling over the advertised tool schemas", file=out)
            res = await run_llm(client, catalogue, task, record, max_steps, out, llm, model)
        else:
            print("Chooser: built-in policy (no LLM_API_KEY set); each next tool is picked from the previous "
                  "result", file=out)
            res = await run_policy(client, catalogue, task, record, max_steps, out)
        res["tools"] = list(catalogue)
        return res


def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--idea", help="a proposed Python idea, e.g. 'add a do-while loop'")
    g.add_argument("--topic", type=int, help="a discuss.python.org Ideas topic id")
    ap.add_argument("--record", action="store_true", help="let triage_topic write its decision (default: dry run)")
    ap.add_argument("--max-steps", type=int, default=8)
    ap.add_argument("--no-llm", action="store_true", help="use the policy even if LLM_API_KEY is set")
    a = ap.parse_args()
    task = {"topic": a.topic} if a.topic is not None else {"idea": a.idea}
    asyncio.run(run(task, record=a.record, max_steps=a.max_steps, use_llm=not a.no_llm))


if __name__ == "__main__":
    main()
