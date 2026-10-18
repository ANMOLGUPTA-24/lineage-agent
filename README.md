# Lineage: a company brain for the Python project

**Has Python already decided this?** Paste an idea and Lineage answers from a FalkorDB graph of 25 years of
Python's decisions: the PEP that proposed it, the ruling and who made it, whether the idea came back later,
the sentence from the source that says so, and who to ask. It can draft the reply. Every answer comes with
the graph path that produced it and the exact Cypher that ran.

Built for **Graph Hacks: Context for AI Agents** (WeMakeDevs × FalkorDB), entered in **Track 01: Agents That
Act on Connected Data** and **Track 03: Company Brain**. The company is the Python project: one of the largest
open-source organisations, with its decisions in public PEPs, forum threads and Steering Council issues. For
Track 01, a triage agent reads new forum ideas, decides from the graph what to do with each, and records and
drafts that action for a person to send ([details](#track-01-an-agent-that-decides-and-acts)).

> **Live demo:** [anmolgupta-24.github.io/lineage-agent](https://anmolgupta-24.github.io/lineage-agent/demo/) (67 answers and 158 triage decisions recorded from the graph: 20 live, the rest from a backfill over forum history; run it locally to ask anything) ·
> **Write-up:** [Python already said no: tracing 26 years of rejected ideas with a graph](https://medium.com/@anmolgupta.skp/python-already-said-no-tracing-26-years-of-rejected-ideas-with-a-graph-d8060ecc1cba) ·
> **Demo video:** [YouTube (3:18, narrated, captioned)](https://youtu.be/-A4iY44PgGk) · [mp4](https://cdn.jsdelivr.net/gh/ANMOLGUPTA-24/lineage-agent@a5a6211c6f20862a9e67b25feda9244ebb528da2/docs/video/lineage-demo.mp4) ·
> **Run it locally:** `docker compose -f docker-compose.quick.yml up --build` → http://localhost:8000 (pre-built graph, no API key;
> 2–4 minutes the first time, about 1 GB of images; port taken? set `ports: ["8080:8000"]` in the compose file) ·
> [![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ANMOLGUPTA-24/lineage-agent?quickstart=1)

![The home page opens on a real recorded answer: null coalescing, proposed in PEP 505 (2015, deferred), raised again in PEP 824 (2026) and in two forum threads, with the people and the graph path behind it](docs/screenshots/home.jpg)

### The problem is real

> "Research whether your idea has been proposed before. There are many suggestions that have been made and
> rejected in the past. … Previously rejected ideas are especially unlikely to get accepted."
> — [CPython Developer's Guide, *Changing Python*](https://devguide.python.org/developer-workflow/lang-changes/)

> "The problem is that people have asked it. Then asked it again. And again. And again. … As a maintainer,
> it's not very fun to keep having to say no over and over in slightly different ways."
> — David Lord, replying on [discuss.python.org](https://discuss.python.org/t/why-not-real-anonymous-functions/44513/14)
> to a proposer who had "searched this forum as well as all of the PEPs" and found nothing

It keeps happening: new lambda or anonymous-function syntax alone was proposed in **8 Ideas topics between 2019
and 2026** (355 posts; titles and counts from the forum's own JSON, in `data/raw/discourse.json`).

Keyword search over PEP titles and forum posts misses rejected ideas buried inside other PEPs (1,085 of them)
and ideas phrased differently. Lineage reads the rejected-ideas sections, links them to what they rejected,
and walks those links: "multi-line lambdas" returns the 2023 and 2025 forum threads and the core developer who answered.

### How it maps to the Company Brain brief

| The brief asks for | In Lineage |
|---|---|
| Knowledge from docs, tickets and chats in one graph | PEPs and their sections (docs), CPython PRs and issues (tickets), discuss.python.org threads (chats) |
| Who owns what, and who knows what | `who_knows`: PageRank over who builds on whom, per area; `bus_factor`: WCC with and without the key person |
| Tracing decisions back to their source | `Decision -[MADE_BY]->` person or council, quoted rulings, `REJECTS_APPROACH_OF` chains across PEPs |
| Keeping knowledge current | `make sync`: rebuild beside the live graph, record `Change` events, swap atomically |
| Answers with clear sources | every claim quoted from the source with a deep link; when an LLM writes the answer, a guard rejects any PEP, name or quote the graph didn't return |
| Gets work done | `draft_reply` writes the forum reply, built only from graph rows |

The graph schema and the tools are source-agnostic. Pointing Lineage at an organisation's RFCs or ADRs
(with their "rejected alternatives" sections), its tracker and its chat means writing one parser per source;
the tools that answer "has this team already decided this?" don't change.

### Track 01: an agent that decides and acts

`lineage/triage.py` is a triage agent for new discuss.python.org Ideas topics. For each topic it asks the graph
whether the idea has history, picks one of five actions from what comes back, records the decision in the graph,
and writes the action to an outbox for a person to send. It never posts to the forum.

| Track 01 asks for | In Lineage |
|---|---|
| Multi-hop reasoning | `decide()` calls `already_decided`, which walks topic → PEP or rejected idea → `RESOLVED_BY` ruling → `MADE_BY` decider, plus `SUPERSEDES*` and `REJECTS_APPROACH_OF*1..3` for earlier and later attempts. `reviewers()` goes on from the match: thread → `POSTED_IN` core developers, PEP → `AUTHORED` authors, and PageRank over the area only when the PEP is the same idea or the area is a named topic |
| Graph queries and algorithms as agent tools | `decide()` and `reviewers()` call the same typed tools as the chat agent (`already_decided`, `who_knows`, `draft_reply` via `tools.draft_from_rows`); `nearest_threads()` is a vector-index query joined to `POSTED_IN`; `who_knows` is PageRank over per-area influence projections |
| Tool selection through MCP | `triage_topic(topic_id, record)` and `triage_queue(limit)` in `lineage/mcp_server.py`, next to the 12 existing tools, so an MCP client chooses when to triage and when to look things up itself. Inside `decide()` the order of tool calls is fixed code, not a model's choice |
| Actions based on what the graph shows | the action is a function of the rows: an adopted same-idea PEP, a rejected one, same-idea forum threads only, related history only, or nothing (`decide()`). `record()` writes `(:Triage)-[:ABOUT]->(:Thread)`, `-[:EVIDENCE]->(:PEP)`, `-[:ROUTED_TO {why}]->(:Person)` |
| Explainable results | every decision carries a one-line `reason`, the evidence rows with links, a `why` for each person it routes to, and the graph paths and Cypher count from `already_decided`; `history()` reads decisions back from the graph |

**The five actions**

| Action | When | What it prepares |
|---|---|---|
| `reply_with_ruling` | a PEP or rejected idea is the same idea, and it was ruled on (rejected, withdrawn, deferred, superseded) | a drafted reply quoting the ruling, with earlier and later attempts |
| `point_to_existing` | the same idea is in a Final, Accepted, Active or Provisional PEP | a drafted reply saying where it lives |
| `link_prior_threads` | the same idea is an open Draft PEP (nothing decided yet), or there is no PEP but the forum discussed the same idea before | a drafted reply pointing to that discussion |
| `flag_for_review` | only related history | what to read, and who to ask |
| `route_to_experts` | nothing in the graph is close | the core developers from the nearest earlier threads, labelled "not a match"; if none are close, it says nobody was found |

Every topic is judged as if just posted: `prior()` drops the topic's own thread and anything dated after it, so
a later PEP can't be cited as history.

**What "act" means here.** Three things, in this order: the decision is written into the graph as `Triage` nodes
and edges; a JSON record and a Markdown note (reason, evidence, who to send it to, drafted reply) go to
`data/triage/<topic>.json` and `.md`; and if `TRIAGE_WEBHOOK_URL` is set (a Slack or Discord incoming webhook,
off by default), the note is posted there. A topic already in the graph as `Triage` is skipped on the next run.

The agent does not reply on the forum, and that is deliberate. discuss.python.org is a public community space,
and an automated "this was rejected in 2009" reply to a newcomer is costly when it is wrong (3 of 32 confident
"same idea" matches in the evaluation below name the wrong PEP) and unwelcoming even when it is right. So the
agent does the searching and drafting, and a person reads the note and decides whether to post it.

**Run it**

```bash
make triage                                            # the 10 newest Ideas topics: graph writes + outbox
make triage-dry                                        # same decisions, nothing written
uv run python -m lineage.triage --latest 10            # what `make triage` runs (topics in creation order)
uv run python -m lineage.triage --topic 109384         # one topic (repeatable)
uv run python -m lineage.triage --latest 10 --dry-run  # what `make triage-dry` runs
```

Over MCP: `triage_topic(topic_id, record=False)` returns the decision (with `record=True` it also writes it, and
returns the earlier decision instead if the topic was already triaged); `triage_queue(limit)` lists recorded
decisions, newest first. The web app serves the same list at `GET /api/triage` and shows it as the **Triage
queue** view (`#triage`); the [live demo](https://anmolgupta-24.github.io/lineage-agent/demo/#triage) shows the
19 recorded decisions. In the quick-start container the graph is loaded from `deploy/graph.rdb` at each start,
so decisions recorded there don't survive a restart.

**One real decision** (`data/triage/examples/109384.json`, trimmed):

```json
{
 "action": "flag_for_review",
 "reason": "Only related history (2 items); a person should judge whether it is the same idea.",
 "confidence": 2.03,
 "evidence": [
  {"kind": "rejected_idea", "pep": 661, "title": "Sentinel Values", "match": "related",
   "url": "https://peps.python.org/pep-0661/#use-a-registry-of-per-module-sentinel-names"},
  {"kind": "rejected_idea", "pep": 661, "title": "Sentinel Values", "match": "related",
   "url": "https://peps.python.org/pep-0661/#using-class-objects"}
 ],
 "reviewers": [{"name": "Tal Einat", "why": "author of PEP 661"},
               {"name": "Jelle Zijlstra", "why": "author of PEP 661"}],
 "draft": null
}
```

The topic "Allow automatic naming of sentinels within classes" matched two rejected ideas in PEP 661 (Sentinel
Values) as related, not the same, so the agent asks the PEP's authors to look rather than replying.

**Recorded decisions: 20**, from live runs on recent Ideas topics (`GET /api/triage`): 12 `route_to_experts`
(2 of them with nobody close enough to route to), 7 `flag_for_review` and 1 `reply_with_ruling`. The reply is
"Define a concurrency model (aka memory model) for Python" (topic 108762): the graph matched it to PEP 583, *A
Concurrency Memory Model for Python* (2008), withdrawn by its authors; the agent drafted a reply quoting the PEP and
routed it to the PEP's author, Jeffrey Yasskin (`data/triage/examples/108762.*`). Drafts are rare in practice:
most new ideas have no same-idea record, as the dry run below also shows.

**Backfill over forum history, hand-checked** ([eval/TRIAGE_BACKFILL.md](eval/TRIAGE_BACKFILL.md)): run over the
1,139 Ideas topics posted since January 2024 (`python -m lineage.triage --backfill --since 2024-01-01`, 32 minutes,
median 2.0 s a topic), the agent drafted a reply for 19: an earlier same-idea PEP or forum thread existed. Every one
was labelled by hand: 8 fully right, 7 partly (related history, not the same idea), 4 wrong. The first run was worse
(7 right of 32); the hand check found three policy bugs, which were fixed and the backfill re-run; both runs are in
the report. It also routed 857 topics to at least one person and wrote 21 `SAME_IDEA_AS` and 491 `RELATED_TO` edges
into the graph. A seeded sample of 20 routed topics found no missed same-idea PEP and 2 missed earlier threads.

**Dry run over the 60 most recent Ideas topics** in the cached data (2026-08-19 to 2026-10-16;
[eval/TRIAGE.md](eval/TRIAGE.md), `uv run python -m eval.triage_eval`):

| Action | Topics (of 60) |
|---|---|
| `reply_with_ruling` | 1 |
| `point_to_existing` | 0 |
| `link_prior_threads` | 0 |
| `flag_for_review` | 17 |
| `route_to_experts` | 42 |

48 of 60 were routed to at least one person. Median decision time 1.69 s (max 2.25 s), models warm, one CPU
process. This measures what the agent does, not whether it is right: there are no labels for the correct action.
A hand check of 9 decisions is in the file: the one `reply_with_ruling` (a memory-model topic matched to PEP 583,
withdrawn in 2008) is right; of the 8 newest `flag_for_review`, none should have been a ruling, one is arguably
`link_prior_threads` and two have weak evidence. The 42 routings were not checked for missed matches, and at
least one is known (109406 has an earlier thread the cross-encoder scored below the cutoff), so read them as
"nothing confident found", not "new".

### Does the graph matter? Measured against vector and keyword search on the same data

| 98 questions with known answers (n) | Lineage (graph) | Vector search* | BM25 keyword search* |
|---|---|---|---|
| Did the idea come back later? (3) | **3** | 0 | 0 |
| How are two PEPs related? (4) | **4** | 1 | 2 |
| Who decided it? (6) | **5** | 1 | 1 |
| Has this idea been decided? Right PEP as the top answer, **held-out** set (17) | **13** | 9 | 11 |
| Is it still current? (31 superseded PEPs) | **31** | 29 | 29 |
| Right PEP anywhere in the top 5 (37; Lineage: top answer only) | 29 | **36** | 35 |

\*Same text, same embedding model, each baseline's no-match cutoff tuned on these very questions. The first
three rows are questions a list of text chunks can't represent; the held-out row is the accuracy test, and n is
small. Lineage is wrong on 3 of 32 confident matches; every miss is listed in [eval/RESULTS.md](eval/RESULTS.md).
Details below.

**Scale:** 745 PEPs, 1,085 rejected ideas and 866 quotable sections parsed from the PEP sources, 1,748
discuss.python.org threads, 505 CPython PRs and 460 issues, 257 rulings, 2,175 people, built deterministically
from public data in under 4 minutes (`make ingest`), with a 107-test suite (behaviour, triage and smoke tests).

**What the graph does per answer:** typed traversals over evidence edges extracted from the PEPs' own text
(`REJECTS_APPROACH_OF`, `CITES`), `algo.SPpaths` with hub-aware weights, PageRank and WCC on per-area
projections, and session memory as graph nodes. Every answer lists the steps that ran, draws the subgraph it
walked, and shows the exact Cypher with its parameters.

![Should Python add a switch statement? PEP 3103, rejected with Guido's rejection notice quoted; PEP 622 took it up again and was replaced by PEP 634 (match). The graph path sits beside the evidence](docs/screenshots/already-decided.jpg)

| Did the idea come back? | Who should I ask about typing? | Memory in the graph |
|---|---|---|
| ![Keyword arguments in indexing: PEP 472 (2014), PEP 637 (2020), then PEP 647 and PEP 696 citing it, each from a REJECTS_APPROACH_OF chain](docs/screenshots/came-back.jpg) | ![PageRank over the typing influence graph, with the citations behind it](docs/screenshots/who-knows.jpg) | ![Session, asked, Query, accessed, PEP](docs/screenshots/memory.jpg) |

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
| `already_decided(idea)` | Has Python already decided this? | vector + full-text candidates over PEPs, 1,085 rejected ideas and 1,748 forum threads, scored by a local cross-encoder; then a traversal: ruling, decider, the PEP's own rejection notice (quoted), `SUPERSEDES*`, and earlier or later PEPs whose rejected ideas cite it (`REJECTS_APPROACH_OF*1..3`) |
| `draft_reply(idea)` | Answer a forum proposal | a reply built only from `already_decided` rows: quotes, deep links, who to ask. Nothing is posted |
| *(after every answer)* | "You asked about PEP 572 earlier…" | session memory in the graph: `(:Session)-[:ASKED]->(:Query)-[:ACCESSED]->(:PEP)`; `algo.SPpaths` from this answer's PEP to the nearest PEP asked about earlier; `my_session` answers "what did I ask earlier?". Carried across sync swaps |
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

After the LLM writes its answer, every `PEP NNN`, person name, verdict word ("accepted", "rejected"…),
year and quotation in it is checked against the tool results. If it states something the graph never
returned, the answer is **rejected** and
replaced with one built directly from the rows. The UI shows the rejected text crossed out.
Decisions the source data doesn't attribute (pre-2018 rulings without a named delegate, for example)
are marked `inferred` and drawn dashed.

## Evaluation, briefly

98 questions with known answers (table at the top). The "has this been decided?" set has a **held-out** part
written after the system was built and never tuned on; currency questions are generated from the raw
`peps.json`, not from the graph. The vector baseline gets the same corpus, the same embedding model and a
no-match cutoff tuned on these questions; with no LLM in the loop it is scored on evidence recall (do its top
5 chunks contain the answer?), an upper bound for any LLM answering from them.

What it says: finding the PEP for an idea is a retrieval problem, and vectors are good at it (the right PEP is
in the baseline's top 5 for 97% of ideas). Every question after that is a relationship, and that is where
retrieval stops. Where Lineage is wrong: 3 of 32 confident "same idea" answers name the wrong PEP, several
right answers come out only "related", and two ground-truth labels were widened after the first run (both to
PEPs that themselves proposed the idea; recorded in `eval/questions.jsonl`). The keyword router's 98/98 tool
choices should be read with care: it was written alongside these questions. On the 60 most recent Ideas topics,
Lineage found an earlier same-idea record for 1 and earlier related history for 11 (18%): most new ideas really
are new, and the value is in the ones that aren't ([eval/RECENT_IDEAS.md](eval/RECENT_IDEAS.md)). Every miss:
[eval/RESULTS.md](eval/RESULTS.md). Reproduce: `uv run python -m eval.run`.

## Keeping it current

`make sync` (add `FETCH=1` to pull peps.json and the PEP sources first) rebuilds the graph **beside**
the live one, diffs the two and writes `(:Change {at, kind, detail})-[:CHANGED]->(:PEP)` for new PEPs,
status changes, new supersessions, new rulings and newly linked PRs. It then flips the
`lineage:active:<tenant>` alias with one `SET`. Agent memory (`Finding`) and earlier change history
are carried over; findings written during the rebuild are copied after the flip. It refuses to swap
a build that lost more than 5% of PEPs, a Redis lock stops two syncs running at once, and
`make rollback` points the alias back. The `changes` tool answers "what changed lately?".

Tested under load: 4 reader threads made 22,468 queries during a 2-minute sync, every one saw a
complete graph (745 PEPs, old or new), with 0 errors. A simulated upstream change (a status change,
a new PEP, a new supersession) came out as exactly those 3 `Change` events.


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
(:Thread {title, url, created, posts, text})-[:DISCUSSES | :MENTIONS]->(:PEP)   discuss.python.org
(:Person)-[:STARTED | :POSTED_IN {posts}]->(:Thread)          170 PEP authors matched by name
(:Change {at, kind, detail})-[:CHANGED]->(:PEP)               written by `make sync`
(:Session {id})-[:ASKED]->(:Query {text, at})-[:ACCESSED]->(:PEP)   per-session agent memory
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
| [discuss.python.org](https://discuss.python.org/c/ideas/6) | 2,073 Ideas topics; first post and participants for the 1,551 with 5+ posts, plus the 197 PEP discussion threads (public JSON API, rate-limited, cached) |

Raw API responses are cached in `data/raw/*.json`, so building the graph needs no tokens.

## Run it

**Quickest (about a minute, graph pre-built):**

```bash
docker compose -f docker-compose.quick.yml up --build      # → http://localhost:8000
```

`deploy/graph.rdb` is a saved FalkorDB graph (9 MB) written by the same pinned FalkorDB image the
compose file uses, so nothing is ingested on startup. The **Codespaces** button above runs exactly this.

**Build the graph yourself from the cached public data (about 2 minutes on 8 cores):**

```bash
cp .env.example .env              # optional: add a free LLM key
docker compose up --build         # FalkorDB + ingest + app → http://localhost:8000
                                  # FalkorDB Browser → http://localhost:3000
```

Without Docker for the app:

```bash
docker run -d -p 6379:6379 -p 3000:3000 falkordb/falkordb@sha256:b0bdcac2cbaff989e77cb0610439b7a9289e86de033ccbbd4368b66fb680ae7a
uv sync && make ingest && make app
make test                         # 107 tests against the loaded graph (behaviour, triage, API and demo smoke)
make smoke                        # check a running instance and every link in the docs
make sync                         # rebuild beside the live graph and swap (see "Keeping it current")
uv run python -m eval.run         # the evaluation below
```

A single-container image (FalkorDB on localhost + the app, graph baked in) for any Docker host is in
`deploy/` (`sh deploy/bundle.sh`, then build `build/space`).

### LLM (optional, free options)

**With no key** (the default, and what the demo shows), a keyword router picks the tools and the answer
is built from the graph rows. The graph does the reasoning either way.

With a key, an LLM picks the tools instead and writes the answer, and the guard checks every PEP number,
name and quote it uses against the rows. The loop speaks the OpenAI-compatible chat API (a free Gemini key
from aistudio.google.com, Groq, or a local Ollama model; see `.env.example`). It is covered by tests with a
scripted model; it has not been run against a hosted model for this submission.

### MCP: the agent interface

The web page is one client. The same typed tools are an MCP server, so any agent (Claude Desktop, Claude
Code, Cursor) can use Lineage as its memory of Python's decisions: the same parameterized Cypher (read-only
except `remember`, which saves a finding, and `triage_topic` with `record=True`) and the same paths in every result. Tools: `already_decided`,
`draft_reply`, `why`, `is_current`, `who_knows`, `bus_factor`, `connection`, `changes`, `resolve`, `recall`,
`remember`, `my_session`, and for the triage agent `triage_topic` and `triage_queue`.

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


## License

MIT, see [LICENSE](LICENSE). The PEP texts, Steering Council issues, CPython metadata and discuss.python.org
posts in `data/raw/` belong to their authors and are used here for research and indexing.
