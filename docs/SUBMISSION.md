# Submission fields (Graph Hacks: Context for AI Agents)

Copy into the submission form. Every number here is from the repo; re-check if the code changes.

**Project name:** Lineage (Python decisions): has Python already decided this?

**Track:** Track 01: Agents That Act on Connected Data, and Track 03: Company Brain

**Tagline:** Has Python already decided this? A FalkorDB graph agent over 26 years of Python's decisions.

**Repository:** https://github.com/ANMOLGUPTA-24/lineage-agent

**Blog post:** https://medium.com/@anmolgupta.skp/python-already-said-no-tracing-26-years-of-rejected-ideas-with-a-graph-d8060ecc1cba

**Live demo:** https://anmolgupta-24.github.io/lineage-agent/demo/ (static: the real UI with 67 answers and 20 triage decisions recorded from the
graph, opening on a multi-hop example; anything else shows how to run it locally in a minute)

**Demo video:** https://cdn.jsdelivr.net/gh/ANMOLGUPTA-24/lineage-agent@7a4cbb155b35fda916942f1a202876e7de0f51ba/docs/video/lineage-demo.mp4 (2:51, narrated, captions burned in; `docs/video/lineage-demo.srt` for upload)

**How to run (live deployment alternative):**
`docker compose -f docker-compose.quick.yml up --build` → http://localhost:8000 (2–4 minutes the first time; the graph is
pre-built). Or the "Open in GitHub Codespaces" button in the README. No API key needed.

**Short description (≈50 words):**
Lineage treats the Python project as the company: 745 PEPs, 1,085 rejected ideas, 1,748 discuss.python.org
threads, Steering Council rulings and CPython PRs in one FalkorDB graph. Ask "has anyone proposed X?" and it
answers with the ruling, who made it, whether the idea came back, quotes from the source, and drafts the reply.

**Track 01:** a triage agent (`lineage/triage.py`) takes each new discuss.python.org Ideas topic, asks the graph
whether it has history, and picks one of five actions: reply with the earlier ruling, point to the accepted PEP,
link earlier threads, flag for review, or route to the core developers nearest the topic. It writes the decision
into the graph (`Triage` nodes with `ABOUT`, `EVIDENCE` and `ROUTED_TO` edges) and an outbox note with a drafted
reply; a person decides whether to post it. It never posts to the forum itself, and skips topics it has already
triaged. Also exposed as the MCP tools `triage_topic` and `triage_queue`.

**What it does:**
- `already_decided` / `draft_reply`: finds the PEP, rejected idea or forum thread that matches a proposal, then
  walks the graph for the ruling, decider (flagged when inferred), the PEP's own rejection notice (quoted,
  deep-linked), supersession, and earlier or later PEPs whose rejected ideas cite it. Drafts a forum reply from those rows.
- `why`, `is_current`, `connection`: decision lineage with Rationale quotes, `SUPERSEDES*` chains, weighted
  shortest paths (`algo.SPpaths`) with hub-aware edge costs.
- `who_knows`, `bus_factor`: PageRank and WCC over per-area influence projections built at ingest.
- `changes`: what the last `make sync` changed. Sync rebuilds beside the live graph and swaps an alias atomically.
- Every answer returns the graph paths and the exact Cypher (with parameters) it ran; the model never writes Cypher.
- Triage agent (Track 01): for each new Ideas topic, decides `reply_with_ruling`, `point_to_existing`,
  `link_prior_threads`, `flag_for_review` or `route_to_experts` from the graph, records `(:Triage)-[:ABOUT|EVIDENCE|ROUTED_TO]`,
  writes an outbox note with the reason, evidence and drafted reply (optional webhook); never posts.
  Dry run over the 60 newest Ideas topics: 1 reply_with_ruling (hand-checked, right), 17 flag_for_review,
  42 route_to_experts, median 1.7 s each (eval/TRIAGE.md, 9 decisions hand-checked). 20 decisions recorded from live
  runs, including one drafted reply (topic 108762 matched PEP 583, withdrawn); most new ideas have no same-idea record. `make triage` / `make triage-dry`;
  Triage queue view in the UI and the live demo (#triage). The tool order inside the agent is fixed code; an MCP
  client chooses when to call it.
- Also exposed as an MCP server.

**How FalkorDB is used (the graph does the work):**
Typed property graph with `REJECTS_APPROACH_OF` / `CITES` edges extracted from PEP text; vector and full-text
indexes for candidate retrieval; `algo.pageRank`, `algo.WCC`, `algo.betweenness`, `algo.labelPropagation`,
`algo.SPpaths`; multi-graph topology for tenants and for zero-downtime sync (alias swap between graphs);
read-only parameterized queries at request time.

**Evidence (98-question eval vs a vector-only baseline on the same corpus):**
Held-out "already decided" top answer 76% vs 53%; who decided 83% vs 17%; did the idea come back 100% vs 0%;
how two PEPs relate 100% vs 25%; 3 of 32 confident matches wrong (listed in eval/RESULTS.md).

**Data:** all public: peps.python.org API, python/peps, python/steering-council, python/cpython, discuss.python.org.

