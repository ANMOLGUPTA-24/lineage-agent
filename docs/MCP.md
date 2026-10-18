# Lineage over MCP

Lineage's tools are served by an MCP server, `lineage/mcp_server.py`, which runs over stdio by default
(add `--http` for streamable HTTP). Any MCP client can connect to it. The repo also has its own client,
`scripts/mcp_agent.py`, which uses the server's tool list to choose which tools to call.

Prerequisites: FalkorDB running (`docker compose up -d falkordb`) and the graph loaded. The server reads
`FALKORDB_HOST` (default `localhost`), `FALKORDB_PORT` (`6379`), `FALKORDB_USERNAME`, `FALKORDB_PASSWORD`
and `LINEAGE_GRAPH` (`python`). It does not need an LLM key.

In the snippets below, replace `/absolute/path/to/lineage` with your checkout. `uv run --directory` sets
the server's working directory (its cwd), so the configs work whichever directory the client starts in.

## Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS),
`%APPDATA%\Claude\claude_desktop_config.json` (Windows), `~/.config/Claude/claude_desktop_config.json` (Linux):

```json
{
  "mcpServers": {
    "lineage": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/lineage", "python", "-m", "lineage.mcp_server"],
      "env": {
        "FALKORDB_HOST": "localhost",
        "FALKORDB_PORT": "6379",
        "LINEAGE_GRAPH": "python"
      }
    }
  }
}
```

If Claude Desktop can't find `uv`, set `"command"` to its absolute path (from `which uv`, e.g. `/home/you/.local/bin/uv`).

## Claude Code

Add it from the CLI:

```bash
claude mcp add lineage -e FALKORDB_HOST=localhost -e FALKORDB_PORT=6379 -e LINEAGE_GRAPH=python \
  -- uv run --directory /absolute/path/to/lineage python -m lineage.mcp_server
```

or check a `.mcp.json` file into the project root:

```json
{
  "mcpServers": {
    "lineage": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/lineage", "python", "-m", "lineage.mcp_server"],
      "env": { "FALKORDB_HOST": "localhost", "FALKORDB_PORT": "6379", "LINEAGE_GRAPH": "python" }
    }
  }
}
```

Use `claude mcp list` or `/mcp` to confirm that `lineage` is connected and lists 14 tools.

## Cursor

`~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project):

```json
{
  "mcpServers": {
    "lineage": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/lineage", "python", "-m", "lineage.mcp_server"],
      "env": { "FALKORDB_HOST": "localhost", "FALKORDB_PORT": "6379", "LINEAGE_GRAPH": "python" }
    }
  }
}
```

## The 14 tools

This table was generated from the server's `tools/list` response, so the signatures and descriptions
match what clients receive. Every result also contains `outcome`, `rows`, `paths` (the graph paths behind
the answer) and `note`. An outcome of `not_in_graph` means the graph has no record of something, not that
it is false.

| Tool | What it does |
|---|---|
| `already_decided(idea)` | Has Python already decided this idea? The PEP, rejected idea or forum thread that matches it, the ruling and who made it, earlier and later PEPs that took it up, quoted and deep-linked. |
| `draft_reply(idea)` | Draft a forum reply to someone proposing `idea`, built only from already_decided's rows. Nothing is posted. |
| `changes(since='', pep='')` | What changed in the graph at recent syncs: new PEPs, status changes, supersessions, rulings, linked PRs. |
| `my_session(session)` | What was asked earlier in a session, with the PEPs each answer touched (read from the graph). |
| `triage_topic(topic_id, record=False)` | Decide what to do with a discuss.python.org Ideas topic from what the graph shows: reply with the earlier ruling, link prior threads, flag for review, or route to the people the area builds on. With record=True the decision is written to the graph and the outbox (nothing is posted). |
| `triage_queue(limit=20)` | The triage agent's recent decisions, newest first, read from the graph. |
| `resolve(text)` | Find PEPs, people or topics matching free text, e.g. 'walrus operator'. |
| `why(pep, depth=2)` | Decision lineage of a PEP: authors, ruling and who made it, earlier PEPs it builds on (depth 1-4), implementing CPython PRs, and anything that superseded it. |
| `is_current(pep)` | Whether a PEP still describes Python, following the SUPERSEDES chain. |
| `who_knows(subject, limit=5)` | People who know a topic (typing, packaging, governance, release) or a PEP's area. |
| `bus_factor(subject)` | Knowledge concentration in a topic or PEP area, and what is orphaned if the top person left. |
| `connection(a, b)` | Shortest weighted path between two PEPs and/or people (FalkorDB algo.SPpaths). |
| `recall(pep)` | Findings saved about a PEP in earlier sessions. |
| `remember(pep, text, session='mcp')` | Save a finding about a PEP for future sessions. |

`triage_topic` defaults to `record=False`, which is a dry run. Only `record=True` writes the decision to the
graph and the outbox, and even then nothing is posted to the forum.

## Tool selection by an MCP client: `scripts/mcp_agent.py`

```bash
uv run python scripts/mcp_agent.py --idea "add a do-while loop"
uv run python scripts/mcp_agent.py --topic 108762            # dry run
uv run python scripts/mcp_agent.py --topic 108762 --record   # also records the triage decision
```

The agent starts the server over stdio (`sys.executable -m lineage.mcp_server`) using the `mcp` package's
`Client` and `StdioServerParameters`, then calls `list_tools()`. It only calls tools that appear in that
list, and it checks each call's arguments against the tool's advertised `inputSchema`. Two choosers are
available:

* **An LLM**, when `LLM_API_KEY` is set. It follows the same convention as `lineage/agent.py`: an
  OpenAI-compatible endpoint configured with `LLM_API_KEY`, `LLM_BASE_URL` and `LLM_MODEL`. The tool
  schemas from `list_tools()` are passed to the model as function definitions. The model picks each call,
  sees the result, and decides what to call next or when to stop. `triage_topic` is forced to
  `record=False` unless `--record` was passed.
* **A small policy**, when no key is set (the trace says which chooser it used). The first call depends on
  the task: `already_decided` for an idea, `triage_topic(record=False)` for a topic. After that, each
  structured result can add follow-up calls:
  * A matched PEP leads to `is_current` and `why`.
  * A `came_back`, `superseded_by` or `latest` PEP leads to the same two calls on that PEP.
  * The people named by `why` lead to `who_knows`.
  * A suggested reviewer, or the person `who_knows` ranks first, leads to `connection` between them and the PEP.
  * A topic with no PEP evidence leads to `resolve` on the topic's title.
  * An idea with no match leads to `resolve` and `who_knows`.

  The run ends with `draft_reply` for an idea that has a prior ruling, or with `triage_topic(record=True)`
  if `--record` was passed for a topic. No call sequence is fixed in advance. Which tools run depends on
  what the results contain.

`tests/test_mcp_agent.py` starts the server over stdio. It checks that the advertised tool list includes
`already_decided`, `triage_topic` and `triage_queue`, and that a policy run for "add a do-while loop"
calls `already_decided` first and then follows up on PEP 315. It also exercises the function-calling path
with a stub OpenAI-compatible client.

### Example trace (a): `--idea "add a do-while loop"` (real output, policy mode)

```
Connected to MCP server 'lineage' over stdio; it advertises 14 tools: already_decided, draft_reply, changes, my_session, triage_topic, triage_queue, resolve, why, is_current, who_knows, bus_factor, connection, recall, remember
Chooser: built-in policy (no LLM_API_KEY set); each next tool is picked from the previous result

[1] already_decided(idea='add a do-while loop')
    why: task is an idea; first check whether Python already decided it
    ->  outcome=answered; best match PEP 315 'Enhanced While Loop' (Rejected), match=same, verdict=rejected

[2] is_current(pep='315')
    why: already_decided matched PEP 315 'Enhanced While Loop' (Rejected) (match=same); is it still how Python works?
    ->  outcome=answered; 1 row(s), 0 path(s); PEP 315 is Rejected; it never became part of Python.

[3] why(pep='315')
    why: already_decided matched PEP 315 'Enhanced While Loop' (Rejected) (match=same); get the ruling and who made it
    ->  outcome=answered; PEP 315 'Enhanced While Loop' (Rejected) by Raymond Hettinger, W Isaac Carroll; ruling: "Subsequent efforts to revive the PEP in April 2009 did not meet with success because no sy..."

[4] who_knows(subject='PEP 315')
    why: why lists Raymond Hettinger, W Isaac Carroll as PEP 315's people; who else knows this area?
    ->  outcome=answered; Raymond Hettinger, R David Murray, W Isaac Carroll

[5] connection(a='Raymond Hettinger', b='PEP 315')
    why: who_knows ranks Raymond Hettinger first; confirm the graph path from them to PEP 315
    ->  outcome=answered; 1 hop(s), strength=direct

[6] draft_reply(idea='add a do-while loop')
    why: a prior ruling exists; draft the reply (nothing is posted)
    ->  outcome=answered; drafted a 556-char reply citing PEP 315

Decision: Already decided: PEP 315 'Enhanced While Loop' (Rejected), verdict rejected (not part of Python). Reply with the ruling. People to route to: Raymond Hettinger, R David Murray, W Isaac Carroll.

Draft reply (not posted):
  This has come up before.
  
  - PEP 315 ("Enhanced While Loop", 2003) proposed it; it was rejected. Its Notice: "Subsequent efforts to revive the PEP in April 2009 did not meet with success because no syntax emerged that could compete with the following form: while True: <setup code> if not <condition>: break <loop body>" https://peps.python.org/pep-0315/#notice
  
  A new proposal would need to answer the reasons above. People who know this history: Raymond Hettinger, W Isaac Carroll.
  
  (Drafted by Lineage from the PEP graph. Check the links before posting.)
```

### Example trace (b): `--topic 108762` (real output, policy mode, dry run)

```
Connected to MCP server 'lineage' over stdio; it advertises 14 tools: already_decided, draft_reply, changes, my_session, triage_topic, triage_queue, resolve, why, is_current, who_knows, bus_factor, connection, recall, remember
Chooser: built-in policy (no LLM_API_KEY set); each next tool is picked from the previous result

[1] triage_topic(topic_id=108762, record=False)
    why: task is a discuss.python.org topic; triage_topic (dry run) reads it and the graph's evidence
    ->  action=reply_with_ruling (confidence 3.66): PEP 583 (A Concurrency Memory Model for Python, 2008) covered the same idea; it was withdrawn by its authors.

[2] is_current(pep='583')
    why: triage evidence cites PEP 583 (match=same); is it still how Python works?
    ->  outcome=answered; 1 row(s), 0 path(s); PEP 583 is Withdrawn; it never became part of Python.

[3] why(pep='583')
    why: triage evidence cites PEP 583 (match=same); get the ruling and who made it
    ->  outcome=answered; PEP 583 'A Concurrency Memory Model for Python' (Withdrawn) by Jeffrey Yasskin

[4] connection(a='Jeffrey Yasskin', b='PEP 583')
    why: triage suggests Jeffrey Yasskin (author of PEP 583); check how they connect to PEP 583
    ->  outcome=answered; 1 hop(s), strength=direct

[5] who_knows(subject='PEP 583')
    why: why lists Jeffrey Yasskin as PEP 583's people; who else knows this area?
    ->  outcome=answered; Jeffrey Yasskin

Decision: reply_with_ruling: PEP 583 (A Concurrency Memory Model for Python, 2008) covered the same idea; it was withdrawn by its authors. People to route to: Jeffrey Yasskin. [dry run, not recorded (pass --record to write it)]

Draft reply (not posted):
  This has come up before.
  
  - PEP 583 ("A Concurrency Memory Model for Python", 2008) proposed it; it was withdrawn by its authors. From the PEP: "This PEP describes how Python programs may behave in the presence of concurrent reads and writes to shared variables from multiple threads. We use a *happens before* relation to define when variable accesses are ordered or concurrent." https://peps.python.org/pep-0583/
  
  A new proposal would need to answer the reasons above. People who know this history: Jeffrey Yasskin.
  
  (Drafted by Lineage from the PEP graph. Check the links before posting.)
```
