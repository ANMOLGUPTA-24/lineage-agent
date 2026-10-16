# Submission fields (Graph Hacks: Context for AI Agents)

Copy into the submission form. Every number here is from the repo; re-check if the code changes.

**Project name:** Lineage

**Track:** Track 03: Company Brain

**Tagline:** Has Python already decided this? A FalkorDB graph agent over 25 years of Python's decisions.

**Repository:** https://github.com/ANMOLGUPTA-24/lineage-agent

**Demo video:** https://github.com/ANMOLGUPTA-24/lineage-agent/blob/master/docs/video/lineage-demo.mp4

**How to run (live deployment alternative):**
`docker compose -f docker-compose.quick.yml up --build` → http://localhost:8000 (about a minute; the graph is
pre-built). Or the "Open in GitHub Codespaces" button in the README. No API key needed.

**Short description (≈50 words):**
Lineage treats the Python project as the company: 745 PEPs, 1,085 rejected ideas, 1,748 discuss.python.org
threads, Steering Council rulings and CPython PRs in one FalkorDB graph. Ask "has anyone proposed X?" and it
answers with the ruling, who made it, whether the idea came back, quotes from the source, and drafts the reply.

**What it does:**
- `already_decided` / `draft_reply`: finds the PEP, rejected idea or forum thread that matches a proposal, then
  walks the graph for the ruling, decider (flagged when inferred), the PEP's own rejection notice (quoted,
  deep-linked), supersession, and later PEPs that set the same approach aside. Drafts a forum reply from those rows.
- `why`, `is_current`, `connection`: decision lineage with Rationale quotes, `SUPERSEDES*` chains, weighted
  shortest paths (`algo.SPpaths`) with hub-aware edge costs.
- `who_knows`, `bus_factor`: PageRank and WCC over per-area influence projections built at ingest.
- `changes`: what the last `make sync` changed. Sync rebuilds beside the live graph and swaps an alias atomically.
- Every answer returns the graph paths and the exact Cypher (with parameters) it ran; the model never writes Cypher.
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

**AI use disclosure:** Built with Claude Code as a pair programmer; graph model, tool design, honesty rules and
data choices decided by the author, every query reviewed and run. See the README section of the same name.
