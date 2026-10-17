# Has Python already decided this? Tracing 26 years of PEPs with a graph

*Who proposed it, who decided, and did it come back? A FalkorDB graph over Python's design history.*

The `do`-`while` loop comes up regularly on discuss.python.org, and that's reasonable: the reasons it was turned
down are hard to find. PEP 315 proposed it in 2003; it was deferred, and Guido rejected it in 2013. That answer
is public, but it sits in one of 745 PEPs (Python Enhancement Proposals, the documents where language changes
are argued and decided), among 1,085 "rejected ideas" entries inside those PEPs, and across 1,748 forum threads.

So I built **Lineage**, an agent you can ask "has Python already decided this?" It answers from a graph, quotes
the source, and shows the path it took.

**Try it:** [live demo](https://anmolgupta-24.github.io/lineage-agent/) · [2:51 video](https://cdn.jsdelivr.net/gh/ANMOLGUPTA-24/lineage-agent@7a4cbb155b35fda916942f1a202876e7de0f51ba/docs/video/lineage-demo.mp4) · [code](https://github.com/ANMOLGUPTA-24/lineage-agent)

## One question, answered

I asked: *"Should Python add a switch statement?"*

![The switch-statement answer: PEP 3103 rejected with Guido's notice quoted, PEP 622 and PEP 634 linked, and the graph path beside it](screenshots/already-decided.jpg)

Lineage found PEP 3103, proposed in 2006, and quoted its rejection notice word for word:

> "A quick poll during my keynote presentation at PyCon 2007 shows this proposal has no popular support. I therefore reject it."

Then it kept walking. PEP 622 (2020) took the idea up again as structural pattern matching, and was replaced
by PEP 634, the `match` statement the Steering Council accepted for Python 3.10. Each of those facts is an edge
in the graph, and each links to the exact section of the PEP that says so. No language model was involved: the
answer is built from the rows the queries returned.

## Why a graph, and not just search

Finding the PEP for an idea is a search problem, and vector search is good at it. Everything people ask next is
about relationships: who decided, whether it was replaced, whether the idea came back, who to ask.

Keyword arguments in indexing (`a[x=1]`) show why. PEP 472 proposed them in 2014 and was rejected. PEP 637
proposed them again in 2020, and the Steering Council rejected it. Two typing PEPs then listed it among *their*
rejected ideas; PEP 696 (2022) says: "Ideally, if PEP 637 wasn't rejected, the following would be acceptable".
No single document contains that chain. The graph does, as three hops:

![Keyword arguments in indexing: PEP 472 (2014), PEP 637 (2020), then PEP 647 and PEP 696 citing it](screenshots/came-back.jpg)

I measured this against a vector-only baseline given the same data and embedding model:

| Question | Lineage | Vector baseline |
|---|---|---|
| Who decided it? | 5 / 6 | 1 / 6 |
| Did the idea come back? | 3 / 3 | 0 / 3 |
| How are two PEPs related? | 4 / 4 | 1 / 4 |

The samples are small, so read these as direction, not precision. The full results, including where Lineage
loses, are further down.

## Three things I learned building it

### 1. Build edges from what the text says, not from what embeds nearby

My first version said PEP 572 (the walrus operator) "builds on" PEP 3150, because it mentions it. It mentions
it to set it aside. Linking similar rejected ideas by embedding distance didn't help either: generic objections
("this adds another way to do things") all look alike to an embedding.

What worked was reading the rejected-ideas sections. About 350 of the 1,085 entries name another PEP. In a
sample of 210 such mentions, the ones pointing at accepted PEPs were context ("PEP 484 already covers this"),
and the ones pointing at older, never-adopted PEPs were earlier competing proposals. Only the second kind
becomes an edge, 50 in all, each keeping the sentence that makes the link:

```cypher
MATCH (a:Alternative)-[:REJECTED_IN]->(p:PEP), (q:PEP {number: c.pep})
WHERE q.type <> 'Process' AND q.created < p.created
  AND q.status IN ['Rejected', 'Withdrawn', 'Deferred', 'Superseded', 'Draft']
MERGE (p)-[e:REJECTS_APPROACH_OF]->(q) ON CREATE SET e.via = [a.url]
```

That edge type is what answers "did it come back?" The `given:` clause from PEP 3150 (Alyssa Coghlan, 2010)
was set aside by PEP 403 in 2011 ("The problems with using a full nested suite are best described in PEP 3150")
and again by PEP 572 in 2018. Following `REJECTS_APPROACH_OF*1..3` also finds the indirect cases, like the
typing PEPs above.

### 2. Vectors find candidates; something else has to decide

For "has this been proposed?", cosine distance could not tell "the same idea" from "the same words". Multi-line
lambdas landed closer to conditional expressions than some correct answers did to their own PEP. No cutoff
works when the wrong answer is nearer than the right one.

So vector and full-text search only gather about 90 candidates: PEPs, rejected ideas and forum threads. A small
cross-encoder (a model that reads the question and one candidate together and scores whether they describe the
same idea; MiniLM, about half a second on a laptop CPU, no API key) keeps the real matches. The graph does the
rest, and sometimes it fixes the model: when a forum thread judged to be the same idea refers to a PEP the model
only called "related", the PEP is promoted. That's how "null coalescing" finds PEP 505.

### 3. Graph algorithms need the right shape

"Who should I ask about typing?" used to be a count of items per person. Now each area gets its own
person-to-person graph at ingest (a → b when a's work in that area builds on, or was decided by, b's), and PageRank
runs on it. FalkorDB's PageRank takes one node label and one relationship type, and in my tests on 4.22 three
parallel edges scored the same as one, so each projection keeps one edge per pair.

The result: for typing, Mark Shannon ranks fourth with a single item, because he was PEP 484's BDFL-Delegate.
The most active person, Jelle Zijlstra with 79 items, is listed separately, because "whose work this area
builds on" and "who is active now" are different questions.

![Who should I ask about typing: PageRank over the typing influence graph, with the citations behind it](screenshots/who-knows.jpg)

Shortest paths needed the same care. With unit weights, the link between PEP 484 and PEP 695 was "Guido wrote
both". Edges into busy nodes now cost more, so the answer is the direct citation:

```cypher
CALL algo.SPpaths({sourceNode: x, targetNode: y, relTypes: $rels, weightProp: 'weight',
                   maxLen: 6, relDirection: 'both', pathCount: 3})
// where person, topic and citation edges cost 1 + ln(1 + degree)
```

## Before the PEP: the forum, and memory

Most ideas are discussed on discuss.python.org and never become PEPs, but the discussion is often the best
record of why. Lineage loads the 2,073 topics in the Ideas category, with the opening post and participants of
the 1,551 that have five or more posts, plus 197 threads where PEPs were discussed. "Have multi-line lambdas
been proposed?" now answers that no PEP did, and points to the 2023 and 2025 threads and the core developer who
replied.

Each answer is also stored as `(:Session)-[:ASKED]->(:Query)-[:ACCESSED]->(:PEP)`. The next answer asks the
graph how its PEP connects to what you asked before (at most two hops, because longer chains link almost
anything), so after the walrus operator, a question about statement-local names ends with "the graph links it
to PEP 572". Because every answer writes to the graph, sessions are capped and requests are rate-limited.

![Session memory as graph nodes](screenshots/memory.jpg)

## Results, including where it loses

98 questions with known answers. The "has this been decided?" set has a **held-out** part I wrote after the
system was built and never tuned on. The baseline is scored on whether its top results contain the answer,
which is a ceiling on any model answering from them.

| Question | n | Lineage | Vector baseline |
|---|---|---|---|
| Held-out ideas: right PEP as the top answer | 17 | 13 | 9 |
| Held-out ideas with no PEP: says "not recorded" | 5 | 5 | 5 (cutoff tuned on these questions) |
| Right PEP anywhere in the top 5 (all ideas) | 37 | 29 (top answer only) | 36 |
| Is it still current? | 31 | 31 | 29 |
| Who decided it? | 6 | 5 | 1 |
| Did the idea come back? | 3 | 3 | 0 |
| How are two PEPs related? | 4 | 4 | 1 |

Median answer time is 0.7 s against the baseline's 0.04 s: the graph walk and the reranker cost something.

The baseline is better at one thing: getting the right PEP somewhere into its top five. Lineage's advantage is
in ranking it first and in everything after. It is also wrong in ways I can name: 3 of 32 confident "same idea"
answers cited the wrong PEP (an HTTP/3 client matched PEP 476, which is about HTTPS certificate checks), and
several correct answers came out only as "related". Every miss is listed in `eval/RESULTS.md`.

When a language model writes the answer instead of the templates, a guard checks every PEP number, person name,
verdict ("accepted", "rejected"), year and quotation against the tool results, and rejects the answer if any of
them didn't come from the graph.

## Keeping it current

`make sync` rebuilds the graph next to the live one, records what changed (new PEPs, status changes, rulings),
carries session memory across, and switches an app-level alias key that every request resolves. With four
threads querying during a two-minute sync, all 22,468 reads saw a complete graph.

## What I'd do next

- Track ideas that came back as new PEPs after 2019, and what changed between the attempts.
- Classify each rejected-idea mention instead of using a status rule, and measure it on the 210-mention sample.
- A larger held-out question set: some rows above rest on three to six questions.
- Use forum participation, not only PEPs and pull requests, when ranking who to ask.

## Built with

Python 3.12, FalkorDB (graph, vector and full-text indexes, `algo.pageRank`, `algo.WCC`, `algo.SPpaths`), FastAPI,
fastembed for local embeddings and reranking, and an MCP server that gives any agent the same 12 tools. It runs
without an API key; any OpenAI-compatible model can be plugged in. Built for Graph Hacks: Context for AI Agents
(WeMakeDevs × FalkorDB), Track 03, Company Brain.

Run it yourself in a few minutes: `docker compose -f docker-compose.quick.yml up --build`, then open
http://localhost:8000.
