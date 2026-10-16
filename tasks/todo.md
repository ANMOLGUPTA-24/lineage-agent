# Lineage: an agent that answers "why does Python work this way?"

Graph Hacks (WeMakeDevs × FalkorDB). **Track 3: Company Brain.** Deadline **Oct 18, 11:59 PM IST**.

The "company" is the Python language project: people, PEPs (proposals), decisions,
discussions, CPython issues/PRs and releases. All data is public.

## Data sources (public, checked)
- `https://peps.python.org/api/peps.json`: 745 PEPs with authors, status, type, created,
  python_version, resolution URL, `requires` / `replaces` / `superseded_by`, discussions_to.
  Includes 25 Superseded, 132 Rejected and 259 PEPs with a resolution link.
- `python/peps` repo raw `.rst`: PEP-Delegate / BDFL-Delegate headers, plus PEP→PEP mentions
  in the body (`REFERENCES` edges).
- CPython GitHub (via `gh api` search, cached to JSON): PRs/issues that mention "PEP NNN",
  their authors and labels. Capped at roughly 30 important PEPs so the rate limit holds.
- `python/steering-council` public issues: Steering Council decisions on PEPs.

## Graph model
```
(:Person {name})-[:AUTHORED]->(:PEP {number, title, status, type, created, python_version, url})
(:Person)-[:DELEGATE_FOR]->(:PEP)                 decided it
(:PEP)-[:SUPERSEDES {weight}]->(:PEP)
(:PEP)-[:REQUIRES {weight}]->(:PEP)
(:PEP)-[:REFERENCES {weight}]->(:PEP)             body mentions
(:PEP)-[:RESOLVED_BY]->(:Decision {url, outcome, at, body})
(:Decision)-[:MADE_BY]->(:Person | :Council)
(:PEP)-[:TARGETS]->(:Release {version})
(:PEP)-[:IN_TOPIC]->(:Topic {name})               typing, packaging, governance…
(:PR|:Issue {number, title, url, merged_at})-[:IMPLEMENTS|:DISCUSSES]->(:PEP)
(:Person)-[:OPENED]->(:PR|:Issue)
(:Finding {text, at, session})-[:ABOUT]->(:PEP)   agent memory
```
Every edge gets a `weight`. Vector index on `PEP.embedding` (title + abstract), used only to
resolve fuzzy names ("walrus" → PEP 572), never to answer questions.

## Agent tools (fixed, typed, exposed over MCP; the model never writes Cypher)
1. `resolve(text)`: finds a PEP, person or topic by number, full-text search or vector
   search. Returns "Unknown entity" if nothing matches.
2. `why(pep, depth≤4)`: decision lineage, i.e. the PEPs it requires or references, the
   decision, who decided it, and the PRs that implemented it. Returns the paths.
3. `is_current(pep)`: follows `SUPERSEDES*` to the current PEP and reports whether the
   knowledge is stale.
4. `who_knows(topic|pep)`: experts ranked by betweenness/PageRank on the author + PR subgraph.
5. `bus_factor(topic)`: drops the top person and checks with WCC whether the topic's
   PEPs/PRs fall apart.
6. `connection(a, b)`: `algo.SPpaths` between any two entities ("how is PEP 484 related to
   PEP 695?").
7. `recall(pep)` / `remember(...)`: Finding nodes, read at the start of each session.

Answer outcomes: answered with a path · answered from memory · not in graph · unknown entity.
A guard checks that every PEP number or name in the LLM's sentence appears in the tool rows.
Otherwise it falls back to a sentence built from the rows.

## Stack
Python 3.12 · uv · `falkordb` client · FastMCP (MCP server) · FastAPI + one small
HTML page with a graph visual of the path (vis-network or cytoscape) · LLM via any OpenAI-compatible endpoint
(free: Gemini API key, Groq or local Ollama; with no key it falls back to sentences built from the rows) ·
local embeddings via fastembed (free) · docker compose (falkordb + app) · deploy on FalkorDB Cloud + Render/Fly.

## Plan
### Day 1 (Oct 16–17): data + graph
- [x] Scaffold repo, docker compose with FalkorDB, uv project
- [x] `ingest/peps.py`: JSON API → PEP, Person, Release, SUPERSEDES/REQUIRES/REPLACES
- [x] `ingest/rst.py`: delegates, body references, abstracts, topics
- [x] `ingest/cpython.py`: PRs/issues mentioning important PEPs (cached JSON)
- [x] `ingest/council.py`: Steering Council decisions
- [x] Indexes: number, full-text title, vector embedding
- [x] Sanity-check queries in FalkorDB Browser
### Day 2 (Oct 17): agent
- [x] `tools.py`: 7 parameterized Cypher tools + algorithm calls, using `ro_query`
- [x] MCP server (FastMCP) exposing the tools
- [x] Agent loop with tool calling, a trace (tool, args, latency, rows) and the guard
- [x] Memory: Finding nodes, recalled at session start; one graph per tenant via `select_graph`
- [x] Tests: unknown entity, empty traversal, superseded chain, guard rejection
### Day 3 (Oct 18): ship
- [x] Web UI: chat + path graph + trace panel
- [ ] Deploy (FalkorDB Cloud + Render/Fly)
- [ ] README: schema diagram, every Cypher query, setup, AI-use disclosure
- [ ] Demo video under 3 min (depth 1 vs depth 4; "superseded"; "graph doesn't know")
- [ ] Blog post (AirPods side prize)
- [ ] Submit before 11:59 PM IST

## Competitive research (Oct 16)
- 30+ public Graph Hacks repos; 5+ in Company Brain (DecisionDebt, Node & Nerve, SetuBrain, KnowGraph, citadel).
- The strongest (DecisionDebt) has a live demo, video, CI, React UI, source-span claims and approval drafts.
- **All of them use fictional companies (~100 nodes). Our edge is real data at scale.**
- Judges (FalkorDB judge at an earlier hackathon) value: honest answers, verifiable results, engineering safeguards.

## Round 2 plan: close the gaps, widen the lead
### A. Answers quoted from the sources (verifiable)
- [ ] Parse PEP sections (Motivation, Rationale, Rejected Ideas) into `(:Section {name, text, url#anchor})`
- [ ] Each rejected idea becomes `(:Alternative {title, text, url})-[:REJECTED_IN]->(:PEP)`
- [ ] `why` returns the rationale quote and the ruling; new tool `considered(idea)`:
      "Why not X? Was X ever proposed?" finds the rejected alternative and quotes its reason
- [ ] Guard: quoted text must appear word for word in a tool result
### B. Onboarding (the main Company Brain use case)
- [ ] `onboard(topic)`: reading order (prerequisites first, by topological order on REQUIRES/REFERENCES,
      ranked by PageRank), superseded PEPs swapped for their replacements, plus who to ask
- [ ] `whats_new(version)`: what each Python release decided, and who decided it
### C. Keeping knowledge current
- [ ] `make sync`: re-pull sources, diff against the graph, write `(:Change)-[:CHANGED]->(:PEP)` events
- [ ] `changes(since)` tool + "synced N h ago · K changes" in the UI; nightly GitHub Action
### D. Evidence that the graph matters
- [ ] `eval/`: about 60 questions with ground truth, graph agent vs vector-only baseline; table in README
### E. Ship
- [ ] CI (GitHub Actions: FalkorDB service, ingest, tests)
- [ ] One Docker image (FalkorDB + app, graph baked in) on Hugging Face Spaces (free)
- [ ] UI: landing stats, quotes panel, double-click a node to expand its neighbours
- [ ] Demo video script under 3 min; blog post

## Round 3: win plan (Oct 16 night). Supersedes the Round 2 ordering above
### What judges will score (research, Oct 16)
No criteria are published for this event. Past WeMakeDevs events used 6 unweighted criteria:
**Potential Impact · Creativity & Innovation · Technical Excellence · Best Use of FalkorDB · UX · Presentation**
(demo video + README). FalkorDB's write-up of winners praised designs that are "graph-native", that treat "no path found"
as a real answer and that "fail loudly", plus "real, load-bearing use" of the sponsor.
Winners solve **one sharp, concrete job**, never a generic chatbot.
Track 03 focus areas, scored here against what we have:
| Focus area | Status |
|---|---|
| Knowledge from docs, tickets, chats in one graph | docs + tickets ✅ · **chats ❌** (discuss.python.org threads) |
| Who owns / knows what | ✅ who_knows, bus_factor |
| Tracing decisions to source | ✅ paths · ⚠️ "builds on" is wrong for rejected approaches |
| Keeping knowledge current | ⚠️ is_current only; **no sync** |
| Answers with clear sources | ⚠️ links only; **no quotes** (sections.py built but not wired) |
| "...and gets work done" | ❌ Q&A only |

### Pitch (one sharp job)
**"Has Python already decided this?"** Python's Ideas forum keeps re-proposing ideas that are already rejected (braces,
`do-while`, `?.` operators, ...). Lineage answers with the rejected alternative, quoted word for word with a deep link,
the ruling, who decided it, whether it is still current, and who to ask, and **drafts the reply**. The same graph handles
onboarding ("what do I read to understand typing?").

### P0: must ship (by Oct 17 night)
- [~] `git init` commit (done, 4 commits) + public GitHub repo (👤 `gh repo create lineage-agent --public --source . --push`) (git history = evidence of original work during the event)
- [x] Wire `ingest/sections.py` into `make ingest`; embed Alternatives (vector index)
- [x] `considered(idea)`: vector finds the Alternative → graph adds PEP, ruling, decider, is_current, who to ask; returns quote + `#anchor` link
- [~] `why` quotes rejected approaches (done) but not yet its Rationale section (ingested, unused); **guard: every quoted span must appear word for word in a tool row** (done)
- [x] Edge fix: a PEP mentioned inside another PEP's Rejected-Ideas section → `(:PEP)-[:REJECTS_APPROACH_OF]->(:PEP)`, not "builds on"
      (fixes "PEP 572 builds on PEP 3150")
- [x] `connection` weights: AUTHORED cost hub-aware (1+ln(1+#authored)), so 484→695 goes through typing PEPs, not "Guido wrote both"
- [x] `draft_reply(idea)`: a ready-to-post forum reply built only from `considered` rows (human posts it; nothing is posted automatically)
- [x] Router + LLM schemas + tests for all of the above
- [ ] Live deploy: one Docker image (FalkorDB + app, graph baked in) on HF Spaces; LLM key as secret
### P1: strong differentiators (Oct 18 morning)
- [x] `eval/`: about 50 questions with ground truth, graph agent vs vector-only baseline, table in README (proves "graph must matter")
- [x] `make sync` + `(:Change)` events + `changes(since)` tool + "synced N h ago" in UI + nightly GitHub Action (keeping knowledge current)
- [ ] `onboard(topic)`: reading order (topological on REQUIRES/REFERENCES, ranked by PageRank, superseded PEPs replaced) + who to ask
- [ ] Chats: discuss.python.org threads from `discussions_to` → `(:Thread)`, participants feed who_knows (capped, cached)
- [ ] CI (GitHub Actions: FalkorDB service → ingest → tests)
### P2: polish
- [ ] UI: landing stats, quote cards with deep links, example-question chips, node expand on double-click
- [ ] Community names: use the top Topic, not the top-ranked PEP (the "Secrets module" cluster of 63 is a grab-bag)
### Ship (Oct 18, user does the parts marked 👤)
- [ ] README: pitch first, GIF, eval table, schema diagram, queries, AI disclosure
- [ ] Demo video script under 3 min → 👤 record
- [ ] Blog post draft (on a real blog platform, not LinkedIn) → 👤 publish
- [ ] 👤 Submit before 11:59 PM IST, Track 03

### Found while checking (Oct 16)
- The FalkorDB container was started without the compose volume, so the graph was lost on restart (rebuilt; 14/14 tests pass).
- The repo has **zero commits**.
- Rules: "entirely generated using AI without meaningful participant contribution... may be rejected". You must be able to
  explain the model, the queries and the decisions in person.

### Round 3 progress (Oct 17)
- Step 1 `d7e35fb`: CITES (218) / REJECTS_APPROACH_OF (50) from rejected-idea text. Rule from a 210-mention sample:
  citations of Final/Accepted PEPs were context, so only older never-adopted PEPs count as rejected approaches.
- Step 2 `47e0e77`: `already_decided` / `draft_reply`. Vector distance could not separate same idea from same words
  (lambda→PEP 308 at 0.21, switch→3103 at 0.30), so vector + full-text gather candidates and a MiniLM-L-6 cross-encoder
  scores them (same ≥5, related 1–5). Ruling notices (160) ingested as quotable sections. Guard checks quotes word for word.
- Step 3 `7828c15`: per-area `INFLUENCE_<area>` projections → PageRank (`EXPERT_IN`) + WCC with/without the top person.
  Tested: algo.pageRank ignores parallel edges. Excluded group author strings (that removal changed governance's top person).
- 33 tests; clean `make ingest` 1m51s; warm `already_decided` ≈0.85 s.
- Still open: `connection` AUTHORED weights (484→695 via "Guido wrote both"), community naming, deploy, eval, sync, chats.

### Round 3 progress, part 2 (Oct 17)
- `e7fafc0` connection: hub-aware person/topic edge costs; 484→695 is the direct citation now.
- `872ebd6` why quotes Rationale / Motivation / ruling notice.
- `e29434f` eval: 98 questions, held-out split; graph questions 83–100% vs baseline 0–25%; held-out "already
  decided" top answer 76% vs 53%; 3/32 confident matches wrong. Found 66 PEPs with no abstract (fixed).
- sync: alias pointer + rebuild beside + diff → Change events + Finding carry-over + lock + refusal + rollback.
  22,468 concurrent reads during a sync, 0 errors, all complete graphs. Simulated upstream diff exact.
- Possible FalkorDB 4.22.1 issue: long-lived, repeatedly re-ingested graph returned PEP 0 as the endpoint of
  3 IMPLEMENTS edges on an unanchored scan (anchored query correct); fresh build correct. No minimal repro yet.
- Deploy: `deploy/` (one container, graph baked in, FalkorDB on localhost only). Built and run as uid 1000 locally: OK.
  Waiting on: HF login + Space name + LLM key (as Space secrets).
- discuss.python.org: `ingest/discourse.py` (fetch → cache → load); already_decided includes threads when loaded.
  Fetch running (2,073 Ideas topics; ~1,748 threads at the forum's pace). Then: load, eval rerun, sync, bundle, deploy.
- docs/DEMO.md (video script) and docs/BLOG.md (draft) written for the author to edit.
- UI checked in Chrome: fixed stale server after alias change, example chips, ruling-notice paths for edgeless PEPs.
- LLM path not exercised against a real model this session (no key): test before deploying.

## Notes from building
- rst parsing lives in `ingest/peps.py`; derived structure (reverse edges, hub-aware weights,
  PageRank, CDLP communities, betweenness bridge score, indexes, embeddings) in `ingest/derive.py`.
- `why` at depth 4 drifted off-topic through citation chains, so ancestors are now filtered by
  embedding distance (MAX_DRIFT 0.27) and paths through Process PEPs are skipped.
- The LLM is optional: a keyword router plus templated answers work with no key.
- mcp 2.x renamed FastMCP to MCPServer.

## Review
_(filled at the end)_
