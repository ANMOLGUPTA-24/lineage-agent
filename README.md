# Lineage

**Why does Python work the way it does?** Lineage is an agent that answers questions about the
Python language's decisions (who proposed what, who decided it, what it builds on, whether it's
still current, and who to ask) by walking a FalkorDB graph of PEPs, people, rulings and CPython
pull requests. Every answer comes with the graph path that produced it.

Built for **Graph Hacks: Context for AI Agents** (WeMakeDevs × FalkorDB), **Track 03: Company Brain**.
The "company" is the Python project, one of the largest open-source organisations, with 25 years of
decisions on public record.

> Demo video: _link_ · Live: _link_

## Why a graph?

The questions people actually ask about a language are about relationships:

- *Why does pattern matching look like this?* PEP 634 → PEP 622 (superseded) → … → earlier proposals it cites.
- *Is PEP 333 still the WSGI spec?* Follow `SUPERSEDES` to PEP 3333.
- *Who should I ask about typing?* Authors, deciders and implementers across 49 typing PEPs, ranked.
- *If one maintainer left packaging, what would be orphaned?*

Embedding search over PEP text can find a PEP, but it can't tell you that a PEP was superseded,
who ruled on it, or which earlier proposals it builds on four hops back. Those are edges.
**Take FalkorDB out and every tool stops working.** Vector search is only used to turn
"walrus operator" into PEP 572.

## What it does

| Tool | Question | Graph work |
|---|---|---|
| `resolve(text)` | "walrus operator" → PEP 572 | exact number → full-text index → vector index; reports when a match is ambiguous |
| `why(pep, depth≤4)` | Why/how did X come about? | variable-length `REQUIRES\|REFERENCES\|SUPERSEDES*1..4`, cheapest path per ancestor, drift filter by `vec.cosineDistance`; ruling + who made it; implementing PRs |
| `is_current(pep)` | Is X still how Python works? | `SUPERSEDES*` chain to the latest PEP |
| `who_knows(subject)` | Who should I ask? | **PageRank** over the area's own influence graph (`INFLUENCE_<area>`: who the area's PEPs and PRs build on), plus the most active people and, for a PEP, its own authors and decider |
| `bus_factor(subject)` | Where is knowledge concentrated? | PEPs that depend on one person, and **WCC** on the area's influence graph with and without its top person |
| `connection(a, b)` | How are A and B related? | **`algo.SPpaths`** weighted shortest paths over every relationship type |
| `already_decided(idea)` | Has Python already decided this? | vector + full-text candidates over PEPs and 1,085 rejected ideas, scored by a local cross-encoder; then a traversal: ruling, decider, the PEP's own rejection notice (quoted), `SUPERSEDES*`, and later PEPs that set the same approach aside (`REJECTS_APPROACH_OF`) |
| `draft_reply(idea)` | Answer a forum proposal | a reply built only from `already_decided` rows: quotes, deep links, who to ask. Nothing is posted |
| `recall` / `remember` | What did we find last time? | `Finding` nodes linked to the PEP they describe |

The model **never writes Cypher**. It picks a tool and fills typed arguments; each query is
hand-written and parameterized (no string concatenation, so no injection), and runs read-only.

### Four kinds of answer

1. **Answered with a path.** The graph holds the relationships, and the UI draws them.
2. **Answered from memory.** A dated finding from an earlier session.
3. **Not in the graph.** The entity exists but no relationship is recorded. Lineage says
   *"the graph doesn't record one"*, never *"there is none"*.
4. **Unknown entity.** The name doesn't resolve, so no query runs. ("Why does Python have the banana keyword?")

### Honesty guard

After the LLM writes its answer, every `PEP NNN` and person name it mentions is checked against the
tool results. If it mentions something the graph never returned, the answer is **rejected** and
replaced with one built directly from the rows. The UI shows the rejected text crossed out.
Decisions the source data doesn't attribute (pre-2018 rulings without a named delegate, for example)
are marked `inferred` and drawn dashed.

## Graph model

```
(:Person {name, github, bridge})-[:AUTHORED {role}]->(:PEP {number, title, status, type, created,
                                                           python_version, url, abstract, rank,
                                                           community, embedding})
(:Person)-[:DELEGATE_FOR]->(:PEP)
(:PEP)-[:SUPERSEDES]->(:PEP)          from Superseded-By / Replaces headers
(:PEP)-[:REQUIRES]->(:PEP)            from Requires headers
(:PEP)-[:REFERENCES]->(:PEP)          PEP mentions in the body text
(:PEP)-[:RESOLVED_BY]->(:Decision {outcome, url, at, verdict, verdict_url})
(:Decision)-[:MADE_BY {inferred}]->(:Person | :Council)
(:PEP)-[:TARGETS]->(:Release {version})
(:PEP)-[:IN_TOPIC]->(:Topic {name})
(:PEP)-[:IN_COMMUNITY]->(:Community {name, hub, size})       label propagation
(:PR | :Issue {number, title, url, merged})-[:IMPLEMENTS | :DISCUSSES]->(:PEP)
(:Person)-[:OPENED]->(:PR | :Issue)
(:Finding {text, at, session})-[:ABOUT]->(:PEP)              agent memory
(:PEP)-[:HAS_SECTION]->(:Section {kind, title, text, url})   Motivation / Rationale / ruling notice
(:Alternative {title, text, url})-[:REJECTED_IN]->(:PEP)      each rejected idea, deep-linked
(:Alternative)-[:CITES {quote}]->(:PEP)                       the sentence that names another PEP
(:PEP)-[:REJECTS_APPROACH_OF {via}]->(:PEP)                   cited PEP was an older, never-adopted proposal
(:Person)-[:INFLUENCE_<area>{n}]->(:Person)                   per-area projection (ingest only)
(:Person)-[:EXPERT_IN {rank, share, items}]->(:Topic | :Community)
```

Every edge has a `weight`. `REFERENCES` edges cost `1 + ln(1 + in-degree of target)`, so shortest
paths don't route through PEPs that everything cites. Steering Council edges cost 3 for the same reason.

**Algorithms used:** `algo.SPpaths` (connection), `algo.pageRank` (PEP rank, and per-area
expertise over each `INFLUENCE_<area>` projection), `algo.WCC` (bus factor: does the area split
without its top person?), `algo.betweenness` (person bridge score), `algo.labelPropagation`
(communities), plus full-text and vector indexes. FalkorDB's PageRank ignores parallel edges
(tested), so each projection has one edge per pair.

**Why a cross-encoder in `already_decided`:** vector distance alone could not separate "same idea"
from "same words" (multi-line lambdas landed 0.21 from conditional expressions, switch statements
0.30 from the switch PEP). Vectors and full-text only gather candidates; a local MiniLM
cross-encoder scores them: true matches scored 6.0-7.9 on hand-checked ideas, unrecorded ones
below 0. Above 5 is "same idea", 1-5 is labelled "related", and below that the answer is
"the graph doesn't record it", never "it's new". **Every query is listed in [docs/QUERIES.md](docs/QUERIES.md)**, generated
from what the tools actually run.

**Multi-tenant:** each tenant is its own FalkorDB graph (`select_graph`), not a filter clause.

## Data (all public)

| Source | What |
|---|---|
| [peps.python.org/api/peps.json](https://peps.python.org/api/peps.json) | 745 PEPs: status, authors, supersedes/requires, resolution links |
| [github.com/python/peps](https://github.com/python/peps) | PEP-Delegate / BDFL-Delegate, sponsors, abstracts, PEP-to-PEP mentions |
| [github.com/python/steering-council](https://github.com/python/steering-council) | Steering Council rulings on PEPs (closed issues and the final comment) |
| [github.com/python/cpython](https://github.com/python/cpython) | PRs and issues whose title names a PEP (backport bots filtered out) |

Raw API responses are cached in `data/raw/*.json`, so building the graph needs no tokens.

## Run it

```bash
cp .env.example .env              # optional: add a free LLM key
docker compose up --build         # FalkorDB + ingest + app → http://localhost:8000
                                  # FalkorDB Browser → http://localhost:3000
```

Without Docker for the app:

```bash
docker run -d -p 6379:6379 -p 3000:3000 falkordb/falkordb:latest
uv sync && make ingest && make app
make test                         # 33 behaviour tests against the loaded graph
```

### LLM (optional, free options)

Any OpenAI-compatible endpoint works: a Gemini API key (free at aistudio.google.com), Groq, or a
local Ollama model. See `.env.example`. **With no key**, a keyword router picks the tools and the
answer is templated from the rows. The graph does the reasoning either way.

### MCP

The same tools are an MCP server, usable from Claude Desktop, Claude Code or Cursor:

```json
{"mcpServers": {"lineage": {"command": "uv", "args": ["run", "python", "-m", "lineage.mcp_server"],
                            "cwd": "/path/to/lineage"}}}
```

## Layout

```
lineage/ingest/peps.py     PEP API + rst parsing → graph
lineage/ingest/github.py   Steering Council + CPython (fetch → cache → load)
lineage/ingest/derive.py   reverse edges, weights, PageRank, CDLP, betweenness, indexes, embeddings
lineage/tools.py           the tool catalogue (all Cypher lives here)
lineage/agent.py           tool-calling loop, router fallback, guard
lineage/mcp_server.py      MCP server
lineage/server.py, web/    FastAPI + single-page UI (Cytoscape.js)
tests/                     outcomes, depth, supersession, tenant isolation, guard
```

## Limitations

- `REFERENCES` comes from text mentions, so "PEP 572 mentions PEP 3150" may mean "rejected the
  approach of", not "builds on". The UI labels these edges *references*, not *depends on*.
- Decision attribution without a named delegate is inferred from the date (BDFL before
  2018-07-12, Steering Council after) and flagged `inferred`.
- CPython PR links come from titles that name a PEP, which covers the main implementation
  PRs but not all of them.

## AI assistance disclosure

Built with Claude Code as a pair-programmer. The graph model, tool design, honesty rules and
data choices were decided by me, and I reviewed, ran and tested every query. Tuning values
(the drift threshold, edge weights) were checked by hand against known PEP histories.
