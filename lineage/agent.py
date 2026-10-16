"""The agent loop: the model picks tools; the graph answers; a guard checks the model's words.

Works with any OpenAI-compatible endpoint (free options: Gemini API key, Groq, local Ollama).
With no key configured, or if the LLM call fails, a keyword router picks the tools and the
answer is built from the rows. The graph does the work either way.
"""
import json
import os
import re
import uuid

from dotenv import load_dotenv

from lineage import tools

load_dotenv()

MAX_STEPS = 6
SYSTEM = """You answer questions about why the Python language works the way it does, using ONLY
the tools, which query a graph of PEPs, people, decisions and CPython PRs.

Rules:
- Never state a fact that is not in a tool result. Never guess PEP numbers or names.
- If the user names something without a PEP number, call `resolve` first. If it returns several
  candidates and none is confident, list them and ask which one they mean.
- At the start, call `recall` for the main PEP to see what earlier sessions found.
- Use `why` for "why/how did X come about", `is_current` for "is X still true/current",
  `who_knows` for "who should I ask / who owns", `bus_factor` for knowledge concentration,
  `connection` for "how are A and B related", `already_decided` for "has X been proposed / why
  not X / was X rejected" or a pasted proposal, and `draft_reply` when asked to answer a proposal.
- Put text inside double quotes only when it is copied word for word from a tool result; the
  guard rejects any quote it cannot find there.
- In already_decided results, match "related" means similar history, not the same idea: say so.
- If a tool's outcome is "not_in_graph", say the graph does not record it. That is NOT
  evidence that something doesn't exist.
- If a decision's attribution is marked inferred, say so ("attributed to").
- Answer in 2-5 plain sentences, cite PEP numbers, and don't repeat the paths;
  the UI shows them."""

SCHEMAS = [
    {"name": "resolve", "description": tools.resolve.__doc__,
     "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "why", "description": tools.why.__doc__,
     "parameters": {"type": "object", "properties": {
         "pep": {"type": "string", "description": "PEP number, e.g. '572'"},
         "depth": {"type": "integer", "minimum": 1, "maximum": 4, "description": "lineage hops; 2 default, 4 for deep history"}},
         "required": ["pep"]}},
    {"name": "is_current", "description": tools.is_current.__doc__,
     "parameters": {"type": "object", "properties": {"pep": {"type": "string"}}, "required": ["pep"]}},
    {"name": "who_knows", "description": tools.who_knows.__doc__,
     "parameters": {"type": "object", "properties": {
         "subject": {"type": "string", "description": "a topic (typing, packaging, governance, release) or a PEP number"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, "required": ["subject"]}},
    {"name": "bus_factor", "description": tools.bus_factor.__doc__,
     "parameters": {"type": "object", "properties": {"subject": {"type": "string"}}, "required": ["subject"]}},
    {"name": "connection", "description": tools.connection.__doc__,
     "parameters": {"type": "object", "properties": {
         "a": {"type": "string", "description": "PEP number or exact person name"},
         "b": {"type": "string", "description": "PEP number or exact person name"}}, "required": ["a", "b"]}},
    {"name": "already_decided", "description": tools.already_decided.__doc__,
     "parameters": {"type": "object", "properties": {
         "idea": {"type": "string", "description": "the proposal in plain words, e.g. 'add a do-while loop'"}},
         "required": ["idea"]}},
    {"name": "draft_reply", "description": tools.draft_reply.__doc__,
     "parameters": {"type": "object", "properties": {"idea": {"type": "string"}}, "required": ["idea"]}},
    {"name": "recall", "description": tools.recall.__doc__,
     "parameters": {"type": "object", "properties": {"pep": {"type": "string"}}, "required": ["pep"]}},
    {"name": "remember", "description": tools.remember.__doc__ + " Only when the user asks you to remember or note something.",
     "parameters": {"type": "object", "properties": {"pep": {"type": "string"}, "text": {"type": "string"}},
                    "required": ["pep", "text"]}},
]


def llm_client():
    key = os.getenv("LLM_API_KEY")
    if not key:
        return None, None
    from openai import OpenAI
    base = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    return OpenAI(api_key=key, base_url=base), os.getenv("LLM_MODEL", "gemini-2.5-flash")


# ---------------------------------------------------------------- guard

def evidence_text(steps: list[dict]) -> str:
    return json.dumps([{k: s.get(k) for k in ("rows", "paths", "note")} for s in steps], default=str)


def evidence_strings(x) -> list[str]:
    if isinstance(x, str):
        return [x]
    if isinstance(x, dict):
        x = list(x.values())
    if isinstance(x, list):
        return [s for v in x for s in evidence_strings(v)]
    return []


QUOTE = re.compile(r"[\"“]([^\"“”]{25,})[\"”]")


def unsupported_quotes(answer: str, steps: list[dict]) -> list[str]:
    """Quoted spans that do not appear word for word in any tool row (whitespace and a trailing
    ellipsis ignored)."""
    norm = lambda s: " ".join(s.split()).rstrip("….").strip()
    ev = [norm(s) for st in steps for s in evidence_strings(st.get("rows", []))]
    return [f'quote "{q[:50]}"' for q in QUOTE.findall(answer) if not any(norm(q) in e for e in ev)]


def guard(answer: str, steps: list[dict]) -> list[str]:
    """Problems with the model's answer: PEP numbers, names or quotes the tools never returned."""
    ev = evidence_text(steps)
    problems = [f"PEP {n}" for n in sorted(set(re.findall(r"PEP[\s-]?(\d{1,4})", answer)))
                if not re.search(rf"\bPEP {n}\b|\"(?:pep|number)\": {n}\b", ev)]
    names = re.findall(r"\b([A-Z][a-z]+(?:\s[A-Z][a-zA-Z'-]+){1,2})\b", answer)
    known_people = {p for s in steps for p in re.findall(r'"(?:name|id|by)": "([^"]+)"', json.dumps(s, default=str))}
    for name in set(names):
        if name not in ev and name.split()[0] in {p.split()[0] for p in known_people} and name not in known_people:
            problems.append(name)
    return problems + unsupported_quotes(answer, steps)


# ---------------------------------------------------------------- templated answers (no LLM)

def template(step: dict) -> str:
    t, o, rows = step["tool"], step["outcome"], step["rows"]
    if o in ("unknown_entity", "not_in_graph", "error"):
        return step["note"]
    r = rows[0] if rows else {}
    if t == "resolve":
        return "Matches: " + "; ".join(f"PEP {x['number']} ({x['title']})" if x["kind"] == "PEP" else f"{x['kind']} {x['name']}" for x in rows)
    if t == "why":
        people = ", ".join(p["name"] for p in r["people"] if p["role"] == "author")
        s = f"PEP {r['pep']} ({r['title']}) is {r['status']}, written by {people}"
        d = r.get("decision")
        if d:
            by = " and ".join(x["by"] for x in d["decided_by"])
            inferred = any(x["inferred"] for x in d["decided_by"])
            s += f"; it was {d['outcome']} {'(attributed to' if inferred else 'by'} {by}{')' if inferred else ''}"
        if x := r.get("rationale"):
            s += f'. Its {x["section"]}: "{x["quote"]}" ({x["url"]})'
        if x := r.get("ruling_notice"):
            s += f'. {x["section"]}: "{x["quote"]}" ({x["url"]})'
        if r.get("builds_on"):
            s += f". It builds on {', '.join(r['builds_on'][:6])}"
        if r.get("earlier_proposals"):
            s += (". Earlier proposals it cites that were never adopted: "
                  + ", ".join(f"{x['pep']} ({x['status']})" for x in r["earlier_proposals"][:6]))
        for x in r.get("rejected_approaches", [])[:2]:
            s += f'. It set aside the approach of PEP {x["pep"]} under "{x["idea"]}": "{x["quote"]}" ({x["url"]})'
        if r.get("superseded_by"):
            s += f". It was later superseded by {', '.join(r['superseded_by'])}"
        return s + "."
    if t == "is_current":
        if r["current"]:
            return f"PEP {r['pep']} ({r['title']}) is {r['status']} and nothing in the graph supersedes it."
        if r["latest"]:
            return (f"PEP {r['pep']} is no longer current: it is superseded, and the latest in the chain is "
                    + ", ".join(f"PEP {x['pep']} ({x['status']})" for x in r["latest"]) + ".")
        return step["note"] or f"PEP {r['pep']} is {r['status']}."
    if t == "who_knows":
        if "people" in r:  # local fallback
            return f"For {r['subject']}: " + "; ".join(f"{p['name']} ({', '.join(x.lower() for x in p['roles'])}, {p['items']} items)" for p in r["people"]) + "."
        s = ""
        if r["direct"]:
            s = "Wrote or decided it: " + ", ".join(f"{d['name']} ({d['role'].replace('_', ' ')})" for d in r["direct"]) + ". "
        s += (f"In {r['subject']} ({r['people_in_area']} people wrote, decided, implemented or discussed its PEPs), "
              f"the work builds most on: "
              + ", ".join(f"{p['name']} ({p['influence_share'] * 100:.1f}% of influence)" for p in r["most_built_on"])
              + ". Most active: " + ", ".join(f"{p['name']} ({p['items']} PEPs/PRs)" for p in r["most_active"]) + ".")
        return s
    if t == "bus_factor":
        m = r["most_involved"]
        who = f"{m['name']}, the person this area's work builds on most," if r.get("connected") else m["name"]
        s = (f"{r['subject']}: {r['peps']} PEPs, {r['people']} people who wrote, decided or implemented them. "
             f"{who} is attached to {m['peps']} of them ({int(m['share'] * 100)}%). "
             f"{r['single_person_peps']} PEPs depend on a single person")
        if r["orphaned_if_they_leave"]:
            s += f"; if {m['name']} left, these would have no one: " + ", ".join(f"PEP {x['pep']}" for x in r["orphaned_if_they_leave"])
        if c := r.get("connected"):
            s += (f". Without {m['name']}, the largest connected group of people goes from {c['largest_group']} to "
                  f"{c['largest_without_them']}" + (f", cutting off {c['cut_off_if_they_leave']}" if c["cut_off_if_they_leave"] else
                                                     ", so no one else is cut off"))
        return s + "."
    if t == "connection":
        p = step["paths"][0]
        return "Shortest link: " + " ".join(x["id"] if "id" in x else (f"-[{x['edge']}]->" if x["forward"] else f"<-[{x['edge']}]-") for x in p) + "."
    if t == "draft_reply":
        return r.get("draft") or step["note"]
    if t == "already_decided":
        out = []
        for x in rows:
            d = x["decision"]
            by = ""
            if d:
                names = " and ".join(y["by"] for y in d["decided_by"])
                by = f" ({'attributed to' if any(y['inferred'] for y in d['decided_by']) else 'by'} {names})"
            head = "Closest related history (not the same idea): " if x["match"] == "related" else ""
            if x["kind"] == "rejected_idea":
                s = (f'{head}PEP {x["pep"]} ({x["title"]}) considered "{x["idea"]}" and set it aside: '
                     f'"{x["idea_quote"]}" ({x["idea_url"]})')
            else:
                s = f"{head}PEP {x['pep']} ({x['title']}, {(x['created'] or '')[:4]}) proposed this; it was {x['verdict']}{by}"
                if x["reason"]:
                    s += f'. {x["reason"]["section"]}: "{x["reason"]["quote"]}" ({x["reason"]["url"]})'
            if x["came_back"]:
                s += ". It came up again in " + ", ".join(f"PEP {b['pep']} ({(b['created'] or '')[:4]})" for b in x["came_back"])
            if x["superseded_by"]:
                s += f". Superseded by {x['superseded_by']}"
            out.append(s + ".")
        return " ".join(out)
    if t == "recall":
        return "Earlier finding: " + "; ".join(f"{x['text']} ({x['at'][:10]})" for x in rows) + "."
    if t == "remember":
        return f"Saved a finding on PEP {r['pep']}."
    return json.dumps(rows)[:300]


ROUTES = [  # (pattern, tool, needs)
    (r"\b(draft|reply|respond)\b", "draft_reply", "idea"),
    (r"\b(already|ever|proposed|suggested|why not|why (doesn't|does not|no|isn't) python|"
     r"been (decided|discussed|rejected)|was there a pep|idea)\b", "already_decided", "idea"),
    (r"\b(remember|note that|save)\b", "remember", "pep"),
    (r"\b(still|current|superseded|outdated|replaced|deprecated)\b", "is_current", "pep"),
    (r"\b(bus factor|leave|left|single point|concentrat)", "bus_factor", "subject"),
    (r"\b(who (knows|owns|should|to ask|decided)|expert)", "who_knows", "subject"),
    (r"\b(connect\w*|relat\w*|link\w*|between)\b", "connection", "pair"),
]


def route(question: str, tenant: str) -> list[dict]:
    steps = []
    depth = 4 if re.search(r"\b(deep|history|origin|roots|all the way)\b", question, re.I) else 2
    topic = re.search(r"\b(typing|packaging|governance|release)\b", question, re.I)
    peps = re.findall(r"PEP[\s-]?(\d{1,4})", question, re.I)

    def find_pep() -> str | None:
        if peps:
            return peps[0]
        subject = re.sub(r"(?i)\b(why|how|did|does|do|is|are|was|the|python|still|current|who|knows|about|"
                         r"what|come|about|have|has|a|an|of|in|to|we|remember|that|deep|history)\b|\?", " ", question)
        res = tools.call("resolve", {"text": " ".join(subject.split())}, tenant)
        steps.append(res)
        top = [r for r in res["rows"] if r["kind"] == "PEP" and r["confident"]]
        return str(top[0]["number"]) if top else None

    for pattern, tool, needs in ROUTES:
        if re.search(pattern, question, re.I):
            if needs == "idea":
                steps.append(tools.call(tool, {"idea": question}, tenant))
            elif needs == "subject":
                subject = topic.group(1).lower() if topic else find_pep()
                if subject:
                    steps.append(tools.call(tool, {"subject": subject}, tenant))
            elif needs == "pair":
                if len(peps) >= 2:
                    steps.append(tools.call(tool, {"a": peps[0], "b": peps[1]}, tenant))
            elif needs == "pep" and (n := find_pep()):
                args = {"pep": n, "text": question, "session": "router"} if tool == "remember" else {"pep": n}
                steps.append(tools.call(tool, args, tenant))
            if steps and steps[-1]["tool"] == tool:
                return steps
    if n := find_pep():
        steps.append(tools.call("recall", {"pep": n}, tenant))
        steps.append(tools.call("why", {"pep": n, "depth": depth}, tenant))
    return steps


def templated_answer(steps: list[dict]) -> str:
    if not steps:
        return "I couldn't find a PEP, person or topic in that question. Try naming a PEP number or a feature."
    useful = [s for s in steps if s["tool"] != "resolve" and s["outcome"] in ("answered", "memory")]
    if useful:
        return " ".join(template(s) for s in useful)
    return template(steps[-1])


# ---------------------------------------------------------------- main entry

def ask(question: str, tenant: str = "python", session: str | None = None, history: list | None = None) -> dict:
    session = session or uuid.uuid4().hex[:8]
    client, model = llm_client()
    if client is None:
        steps = route(question, tenant)
        return {"answer": templated_answer(steps), "mode": "router", "steps": steps, "session": session}

    messages = [{"role": "system", "content": SYSTEM}, *(history or []), {"role": "user", "content": question}]
    steps: list[dict] = []
    try:
        for _ in range(MAX_STEPS):
            resp = client.chat.completions.create(
                model=model, messages=messages, temperature=0,
                tools=[{"type": "function", "function": s} for s in SCHEMAS])
            msg = resp.choices[0].message
            if not msg.tool_calls:
                answer = (msg.content or "").strip()
                problems = guard(answer, steps)
                if problems or not answer:
                    return {"answer": templated_answer(steps), "mode": "guarded",
                            "rejected": {"answer": answer, "unsupported": problems}, "steps": steps, "session": session}
                return {"answer": answer, "mode": "llm", "model": model, "steps": steps, "session": session}
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                if tc.function.name == "remember":
                    res = tools.remember(args.get("pep", ""), args.get("text", ""), session, tenant)
                    res["ms"] = 0
                else:
                    res = tools.call(tc.function.name, args, tenant)
                steps.append(res)
                compact = {k: res[k] for k in ("outcome", "rows", "note")}
                compact["paths"] = [" ".join(x.get("id") or f"-{x['edge']}-" for x in p) for p in res["paths"]]
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(compact, default=str)[:6000]})
        return {"answer": templated_answer(steps), "mode": "guarded", "rejected": {"answer": "", "unsupported": ["step limit"]},
                "steps": steps, "session": session}
    except Exception as e:  # rate limit, network, bad model output: the graph still answers
        steps = steps or route(question, tenant)
        return {"answer": templated_answer(steps), "mode": "router", "error": str(e)[:200], "steps": steps, "session": session}


if __name__ == "__main__":
    import sys
    out = ask(" ".join(sys.argv[1:]))
    print(out["answer"], f"\n[{out['mode']}]")
    for s in out["steps"]:
        print(f"  {s['tool']}({s['args']}) -> {s['outcome']}, {len(s['rows'])} rows, {len(s['paths'])} paths, {s.get('ms')} ms")
