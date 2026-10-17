<!-- Draft for the Graph Hacks blog prize. Written in the first person for the author to edit,
     check and publish on a real blog platform (not a LinkedIn post). Every number here comes from
     the repo (eval/RESULTS.md, tests, commit messages); re-check them if the code changes. -->

# Has Python already decided this? Building a graph agent over 25 years of PEPs

Every few weeks someone on discuss.python.org proposes a `do`-`while` loop. Python decided that in
2003 (PEP 315, rejected). The switch statement was rejected in 2007 after "a quick poll" during Guido's PyCon
keynote; `?.` operators have been deferred since 2015 and proposed again in 2026. The answers exist,
but they are spread across 745 PEPs, their rejected-ideas sections, Steering Council issues and
CPython pull requests.

For Graph Hacks (WeMakeDevs × FalkorDB, track "Company Brain") I built **Lineage**: an agent that
treats the Python project as the company and answers "why does Python work this way?" and "has this
been decided?" by walking a FalkorDB graph. Every answer comes with the path it walked and links to
the exact section of the PEP it quotes.

Code, queries and the evaluation: https://github.com/ANMOLGUPTA-24/lineage-agent

## Why a graph, concretely

Finding the PEP for an idea is a retrieval problem. Everything people ask *after* that is about
relationships:

- Who decided it, and is that recorded or inferred from the date?
- Was it superseded, and by what, and is *that* still current?
- Did the idea come back later, and who rejected it the second time?
- Who should I ask about typing, and what breaks if they leave?

I measured this (details below). Against a vector-only baseline given the same corpus, the same
embedding model and a cutoff tuned on the test questions, the gap on relationship questions was
83% vs 17% (who decided), 100% vs 0% (did the idea come back) and 100% vs 25% (how two PEPs relate).

## The graph

```
(:Person)-[:AUTHORED|DELEGATE_FOR]->(:PEP)-[:RESOLVED_BY]->(:Decision)-[:MADE_BY {inferred}]->(:Person|:Council)
(:PEP)-[:SUPERSEDES|REQUIRES|REFERENCES]->(:PEP)
(:Alternative {title, text, url})-[:REJECTED_IN]->(:PEP)        1,085 rejected ideas, deep-linked
(:Alternative)-[:CITES {quote}]->(:PEP)
(:PEP)-[:REJECTS_APPROACH_OF {via}]->(:PEP)
(:PR|:Issue)-[:IMPLEMENTS|DISCUSSES]->(:PEP)
(:Thread)-[:DISCUSSES|MENTIONS]->(:PEP)                         discuss.python.org
(:Person)-[:EXPERT_IN {rank, share}]->(:Topic|:Community)
(:Change)-[:CHANGED]->(:PEP)
```

The model never writes Cypher. It picks one of a fixed set of tools; each runs hand-written,
parameterized, read-only queries, and returns rows, paths and the exact Cypher with its parameters.

## Three things that only worked once I stopped treating the graph as storage

### 1. Edges built from what the text says, not from what embeds nearby

My first version said "PEP 572 (the walrus operator) builds on PEP 3150, 359 and 403". Those are
mentions in the body, and PEP 572 mentions them because it *rejected* their approaches.

I tried linking similar rejected ideas by embedding distance first. It produced noise: "turn
assignment into an expression" landed next to "the feature adds another way of doing things",
because generic objections all look alike to an embedding.

What worked was reading the rejected-ideas sections. 352 of the 1,085 rejected ideas name another
PEP. In a sample of 210 such mentions, the ones pointing at Final or Accepted PEPs were context
("PEP 484 already covers this"), and the ones pointing at older, never-adopted PEPs were earlier
competing proposals. So `REJECTS_APPROACH_OF` exists only for the second kind, 50 edges, each
carrying the sentence that set the other PEP aside:

> PEP 572, *Alternative spellings*: "See PEP 3150 for prior discussion on this subject (with the
> proposed keyword being `given:`)."

That one edge type answers "did this idea come back?" (3150 → 403 → 572), which no chunk of text
contains.

### 2. Vectors find candidates; they don't decide

For "has this been proposed?", cosine distance could not separate "same idea" from "same words":
multi-line lambdas landed 0.21 from conditional expressions, and switch statements 0.30 from the
switch PEP itself. No cutoff works when the wrong answer is closer than the right one.

So vector and full-text search only gather about 90 candidates (PEPs, rejected ideas, forum
threads), and a small local cross-encoder (MiniLM-L-6, ~0.45 s on CPU, no API key) scores each
(idea, candidate) pair. Then the graph takes over for the part that matters: the ruling, who made
it, the PEP's own rejection notice, supersession, and later PEPs that set the same approach aside.

### 3. Graph algorithms on projections, and reading the docs by testing them

`who_knows` used to count items per person, which is a `GROUP BY`. Now each topic and large
community gets its own Person→Person projection at ingest (`a → b` when a's PEP or PR builds on, or
was decided by, b's work in that area), and `algo.pageRank` runs on it. For typing it ranks Mark
Shannon fourth with one item, because he was PEP 484's decider; the most active person (Jelle
Zijlstra, 79 items) is listed separately, because "who the area builds on" and "who is active now"
are different questions.

Two things I only learned by testing on a scratch graph: FalkorDB's PageRank takes exactly one label
and one relationship type (hence the projections), and it ignores parallel edges (three `a→b`
edges score the same as one), so the projection keeps one edge per pair. `algo.WCC` with and
without the area's top person gives the bus factor: removing the top person from the metaclasses
cluster cuts 3 of 13 people off from the rest.

Shortest paths needed the same care. With unit weights, the shortest link between PEP 484 and PEP 695
was "Guido wrote both", then "both are typing PEPs". Person and topic edges now cost
`1 + ln(1 + degree)`, as citations of heavily cited PEPs already did, and the answer is the direct
citation.

## Keeping it honest

- An entity that doesn't resolve runs no query ("the banana keyword").
- Not finding a relationship is reported as "the graph doesn't record one", never "there is none".
- Decisions attributed by date rather than by a named delegate are marked inferred.
- A guard rejects any LLM answer that names a PEP or person, or quotes text, that the tool rows
  don't contain word for word, and falls back to an answer built from the rows.

## The evaluation, including where it loses

98 questions with known answers. The "has this been decided?" set has a **held-out** part written
after the system was built and never tuned on. Currency questions are generated from the raw PEP
metadata, not from the graph. With no LLM in the loop, the baseline is scored on evidence recall
(do its top 5 chunks contain the answer?), which is an upper bound on any LLM answering from them.

| | Lineage | Vector baseline |
|---|---|---|
| Held-out ideas: right PEP as the top answer | 76% | 53% |
| Held-out ideas with no PEP: correctly says "not recorded" | 100% | 100% (tuned cutoff) |
| Right PEP anywhere in the top 5 | 78% (top answer) | 97% |
| Is it still current? (31 superseded PEPs) | 100% | 94% |
| Who decided it? | 83% | 17% |
| Did the idea come back? | 100% | 0% |
| How are two PEPs related? | 100% | 25% |

The baseline's 97% at top 5 is the honest part of the story: vectors are good at finding the PEP.
Lineage's wins are in ranking it first and in everything after. It is also wrong in ways I can
name: 3 of 32 confident "same idea" answers cited the wrong PEP (an HTTP/3 client matched PEP 476
on HTTPS verification), several correct answers came out as only "related", and two ground-truth
labels were widened after the first run, both to PEPs that themselves proposed the idea. All of
that is listed in `eval/RESULTS.md`.

The evaluation also found a real bug: 66 PEPs have no section called "Abstract", so search had only
their titles. PEP 308 (conditional expressions) was invisible to the reranker until it got its
first section instead.

## Keeping it current without breaking readers

`make sync` rebuilds the graph beside the live one, diffs the two into `(:Change)` events (new
PEPs, status changes, supersessions, rulings, linked PRs), copies agent memory across, and flips a
Redis alias in one `SET`. It refuses a build that lost more than 5% of PEPs and holds a lock so two
syncs can't race. With four threads querying throughout a two-minute sync, all 22,468 reads saw a
complete graph and none failed.

Rebuilding instead of patching turned out to matter. A graph I had re-ingested in place many times
started returning PEP 0 as the endpoint of three edges on an unanchored scan, while the anchored
query returned the right PEP. A fresh build was correct, and the sync diff is what surfaced it. Replaying
four rounds of the same in-place re-ingest didn't reproduce it, so I report it as an observation, not a bug.

## Where ideas go to die: the forum

Most ideas never become PEPs. They are proposed on discuss.python.org, argued over, and dropped. I
pulled the 2,073 topics in its Ideas category (the public JSON API, rate-limited and cached) and the
first post and participants of the 1,551 with five or more replies, plus the 197 threads where PEPs
were discussed. Threads join the candidates for "has this been proposed?", ranked below PEPs (a thread
is where an idea was discussed, not decided), and 170 PEP authors link up with their forum activity
by name. "Have multi-line lambdas ever been proposed?" used to end at "the graph doesn't record it";
it now answers that no PEP did, and points to the 2023 thread and the core developer who replied.

## Memory that lives in the graph

Every answer is recorded as `(:Session)-[:ASKED]->(:Query)-[:ACCESSED]->(:PEP)`. The next answer then
asks the graph how its PEP connects to what the same session asked before (`algo.SPpaths`, at most three
hops), so after "why the walrus operator?" a question about statement-local bindings ends with "Earlier in
this session you asked about PEP 572; the graph links it to this answer: PEP 3150 <-[REJECTS_APPROACH_OF]-
PEP 572." "What did I ask earlier?" is read back from those nodes, and `make sync` carries sessions
across the swap like everything else that isn't rebuilt from source.

The UI also lists, for every answer, the steps that actually ran, written from the results rather than
from a script: which indexes returned how many candidates, what the cross-encoder kept, and what each
traversal found.

## What I'd do next

- Calibrate the "same idea" cutoff on a larger labelled set, where the held-out misses were
  under-confident rather than wrong.
- Use forum participation in `who_knows`, not only PEPs and PRs.
- Track who reversed whom: ideas rejected under the BDFL and accepted by the Steering Council.

## How I built it, and with what

Python 3.12, FalkorDB (graph, vector and full-text indexes, `algo.pageRank`, `algo.WCC`,
`algo.betweenness`, `algo.SPpaths`, `algo.labelPropagation`), FastAPI, an MCP server exposing the
same tools, fastembed for local embeddings and reranking, and any OpenAI-compatible LLM (or none:
a keyword router plus templated answers works without a key).

