# Python already said no: tracing 26 years of rejected ideas with a graph

*745 PEPs, 1,085 rejected ideas, 1,748 forum threads, one FalkorDB graph, and the bugs I found by trying to
break it.*

I kept running into the same pattern on discuss.python.org: someone proposes a `do`-`while` loop, a
`switch` statement or `?.` operators, and the thread fills up with people half-remembering that "this was
rejected years ago". It usually was. PEP 315 proposed `do`-`while` in 2003; it was deferred, and Guido rejected
it in 2013. The answer is public, but it sits in one of 745 PEPs (Python Enhancement Proposals, the documents
where language changes are argued and decided), in one of 1,085 "rejected ideas" entries inside those PEPs, or in
one of 1,748 forum threads.

So for a FalkorDB hackathon (Graph Hacks, WeMakeDevs × FalkorDB) I built **Lineage**, which answers one
question: *has Python already decided this?* It answers from a graph, quotes the source, and shows the path it
walked. Because it was a FalkorDB hackathon, I leaned on FalkorDB's graph algorithms on purpose; this post is about
where that paid off and where it didn't.

**Try it:** [live demo](https://anmolgupta-24.github.io/lineage-agent/) (it replays 67 answers recorded from the
graph; run it locally to ask anything) · [2:51 video](https://cdn.jsdelivr.net/gh/ANMOLGUPTA-24/lineage-agent@a5a6211c6f20862a9e67b25feda9244ebb528da2/docs/video/lineage-demo.mp4) · [code](https://github.com/ANMOLGUPTA-24/lineage-agent)

## What an answer looks like

*"Should Python add a switch statement?"*

![The switch-statement answer: PEP 3103 rejected with Guido's notice quoted, PEP 622 and PEP 634 linked, and the graph path beside it](screenshots/already-decided.jpg)

Lineage finds PEP 3103 (2006) and quotes its rejection notice:

> "A quick poll during my keynote presentation at PyCon 2007 shows this proposal has no popular support. I therefore reject it."

Then it keeps walking. PEP 622 (2020) took the idea up again as structural pattern matching and was replaced by
PEP 634, the `match` statement accepted for Python 3.10. Each claim links to the PEP section it came from. The
answer took 12 queries and about 1.2 seconds on my laptop, and no language model wrote it: the text is filled in
from templates.

## Search finds the PEP; the follow-up questions are about relationships

Finding the PEP for an idea is retrieval, and vector search does it well. The questions people ask next are not
retrieval: who decided, was it replaced, did the idea come back, who should I ask.

Keyword arguments in indexing (`a[x=1]`) are a good example. PEP 472 proposed them in 2014 and was rejected. PEP
637 proposed them again in 2020, and the Steering Council (the five-person group that has decided PEPs since
2019, after Guido stepped down) rejected it too. Then two typing PEPs listed PEP 637 among *their* rejected ideas;
PEP 696 (2022) says "Ideally, if PEP 637 wasn't rejected, the following would be acceptable". No single document
contains that chain, but the graph has it as two hops back from each typing PEP to PEP 472:

![Keyword arguments in indexing: PEP 472 (2014), PEP 637 (2020), then PEP 647 and PEP 696 citing it](screenshots/came-back.jpg)

The queries are written in Cypher, which is SQL for graphs: instead of joining tables, you draw the path you
want with arrows. This one finds every PEP that set aside PEP 472's approach, directly or through another PEP:

```cypher
MATCH back = (later:PEP)-[:REJECTS_APPROACH_OF*1..3]->(p:PEP {number: 472})
RETURN back
```

The core of the schema, all in one graph:

```
(:Person)-[:AUTHORED|DELEGATE_FOR]->(:PEP)-[:RESOLVED_BY]->(:Decision)-[:MADE_BY {inferred}]->(:Person|:Council)
(:PEP)-[:SUPERSEDES|REQUIRES|REFERENCES]->(:PEP)
(:Alternative {title, text, url})-[:REJECTED_IN]->(:PEP)      the 1,085 rejected ideas
(:Alternative)-[:CITES {quote}]->(:PEP)
(:PEP)-[:REJECTS_APPROACH_OF {via}]->(:PEP)
(:Thread)-[:DISCUSSES|MENTIONS]->(:PEP)                        discuss.python.org
(:PR|:Issue)-[:IMPLEMENTS|DISCUSSES]->(:PEP)
```

## 1. Edges from what the text says

My first version told me PEP 572 (the walrus operator) "builds on" PEP 3150. PEP 572 does mention PEP 3150, so the
`REFERENCES` edge was real. But it mentions it to set it aside: "See PEP 3150 for prior discussion on this subject
(with the proposed keyword being `given:`)". A reference is not an endorsement, and the graph couldn't tell the
difference.

I tried linking similar rejected ideas by embedding distance first. It produced noise: generic objections like
"this adds another way to do things" all sit close together, whatever the feature.

What worked was reading the rejected-ideas sections. 179 of the 1,085 entries cite another PEP, 218 citations in
all, and reading through them the pattern was clear. A citation of an accepted PEP is context ("PEP 484 already
covers this"). A citation of an older PEP that was never adopted is an earlier competing proposal. Only the second
kind becomes an edge, and the edge keeps the URL of the entry that justifies it:

```cypher
MATCH (a:Alternative {url: r.url})-[:REJECTED_IN]->(p:PEP)
UNWIND r.cited AS c
MATCH (q:PEP {number: c.pep}) WHERE q <> p
MERGE (a)-[ci:CITES]->(q) SET ci.quote = c.quote
WITH a, p, q WHERE q.type <> 'Process' AND q.created < p.created
               AND q.status IN ['Rejected', 'Withdrawn', 'Deferred', 'Superseded', 'Draft']
MERGE (p)-[e:REJECTS_APPROACH_OF]->(q)
  ON CREATE SET e.via = [a.url]
  ON MATCH SET e.via = e.via + a.url
```

That rule produces 50 edges. PEP 3150's `given:` clause (Alyssa Coghlan, 2010) was set aside by PEP 403 in 2011
and again by PEP 572 in 2018; vectorcall (PEP 590) set aside PEP 580 and PEP 576. And the walrus operator no
longer "builds on" anything it rejected. The rule is a heuristic, though: status and date stand in for reading
each citation, and I haven't measured how often it is wrong.

## 2. Candidates from vectors, decisions from a cross-encoder

For "has this been proposed?", cosine distance could not separate "the same idea" from "the same words".
Multi-line lambdas landed closer to conditional expressions than some correct answers did to their own PEP. No
cutoff works when the wrong answer is nearer than the right one.

So the vector and full-text indexes only gather candidates: up to 15 from each of six searches (vector and
full-text, over PEPs, rejected ideas and forum threads), so up to 90 before de-duplication:

```cypher
CALL db.idx.vector.queryNodes('Alternative', 'embedding', $k, vecf32($v)) YIELD node
RETURN 'alt', node.url, node.title, node.title + '. ' + node.text
```

A cross-encoder decides. Unlike an embedding, it reads the question and one candidate together and scores
whether they describe the same thing. I use `ms-marco-MiniLM-L-6-v2` through fastembed: local, no API key, about
0.45 s for 60 candidates on a laptop CPU, on 300-character snippets. A score of 5 or more counts as "same idea",
1 to 5 as "related". I set those bands by hand on 12 ideas I checked myself: true matches scored 6.0 to 7.9,
loosely related history about 2, ideas with no PEP -0.4 or lower.

Phrasing turned out to matter a lot. The cross-encoder scores "add a do-while loop" at 7.8 against PEP 315, but
"Has anyone proposed a do-while loop?" at about 4, below the cutoff. So the question's framing ("has anyone
proposed", "draft a reply to someone suggesting") is stripped before scoring.

The evaluation also found that 66 PEPs have no section called "Abstract". PEP 308 (conditional expressions) opens
with "Adding a conditional expression", so search and the reranker only ever saw its title. The ingest now falls
back to the first section; only 3 PEPs still have no summary text. That didn't fix PEP 308's own question, which
still misses (more below).

When a forum thread judged to be the same idea refers to a PEP the cross-encoder only called "related", that PEP
is promoted to the same idea. That's how "has anyone proposed null coalescing?" lands on PEP 505 (2015, deferred)
and its 2026 revival, PEP 824, rather than only on the forum threads.

## 3. PageRank and shortest paths need the right shape

"Who should I ask about typing?" started as a count of items per person, which is a `GROUP BY`, not a graph
answer. Now each area gets its own person-to-person graph at ingest: an edge `a → b` when a's PEP or pull request
in that area builds on b's work, and another when b was the delegate who decided a's PEP. PageRank runs on that.

Two things I only learned by testing on a scratch graph: FalkorDB's `algo.pageRank` takes exactly one node label
and one relationship type, and three parallel `a → b` edges score the same as one (FalkorDB 4.22). So each area
gets its own relationship type with one edge per pair:

```cypher
MATCH (q:PEP)-[:IN_TOPIC]->(:Topic {name: 'typing'})
MATCH (b:Person)-[:AUTHORED|DELEGATE_FOR]->(q)
MATCH (a:Person)-[:AUTHORED|OPENED]->(x)-[:REQUIRES|REFERENCES|SUPERSEDES|IMPLEMENTS|DISCUSSES]->(q)
WHERE a <> b
WITH a, b, count(*) AS n
CREATE (a)-[:INFLUENCE_typing {n: n}]->(b)
```
```cypher
CALL algo.pageRank('Person', 'INFLUENCE_typing') YIELD node, score
```

The trade-off: PageRank here is unweighted. Thirteen links from one person to another count the same as one;
`n` is kept only to explain the ranking. Even so, the result says something a count can't. Mark Shannon ranks
fourth in typing with a single typing item, because he was PEP 484's BDFL-Delegate (the person Guido named to
decide it), and a lot of later typing work builds on that decision. Jelle Zijlstra, with 79 typing PEPs and pull
requests, is listed separately as the most active. "Whose work this area builds on" and "who is active now" are
different questions, and the answer shows both.

![Who should I ask about typing: PageRank over the typing influence graph, with the most active people listed separately](screenshots/who-knows.jpg)

Shortest paths had the same problem in another form. With every edge costing 1, the link between PEP 484 and PEP
695 was "Guido wrote both" (cost 2), which beat the direct citation from 695 to 484 (cost 4.3 after weighting).
Guido authored 65 PEPs, so he sits in the middle of every short path. The fix was to make edges out of prolific
people cost more, computed once at ingest:

```cypher
// for AUTHORED, OPENED and DELEGATE_FOR in turn
MATCH (a:Person)-[e:AUTHORED]->() WITH a, count(e) AS n
MATCH (a)-[e:AUTHORED]->() SET e.weight = 1.0 + log(1.0 + n)
```

Topic membership got the same treatment ("both are typing PEPs" is true of 49 PEPs), and Steering Council rulings
cost 3, because the Council made over a hundred of them. `algo.SPpaths` then uses the weights, and the answer is
the direct citation. When the cheapest path is still three or more hops, Lineage says there is no close
relationship instead of presenting a chain through famous PEPs as if it meant something.

## 4. Attacking my own agent

Before submitting, I ran a stress test against the running app: injection attempts, malformed input, 40
concurrent requests, and probes of the guard. Injection failed, because every query takes parameters and the model
never writes Cypher. The rest found five bugs.

**The guard had a hole.** When a language model writes the answer, a guard checks it against the tool results.
It caught invented PEP numbers and quotes, but it only flagged a name if its *first name* matched someone in the
results. "PEP 572 was rejected by Elon Musk in 2010" passed: wrong verdict, invented person, wrong year. The guard
now flags any capitalised name containing a word the evidence never uses, checks status words ("accepted",
"rejected") against each PEP's recorded status, and checks every year. That exact sentence is a test case now.

**"PEP -1" answered about PEP 0.** The number didn't parse, so it fell through to full-text search, which matched
PEP 0, the index of all PEPs. Malformed references now resolve to nothing, and the answer says so.

**An unknown person was silently dropped.** "How is Zaphod Beeblebrox connected to PEP 572?" answered about PEP
572 alone. Connections now resolve both sides or say which one they couldn't find.

**A search index disappeared.** Earlier the same day, "multi-line lambdas" had started returning "not in the
graph". The vector index on forum threads was gone from the live graph; every thread search raised an error, which
my code caught as "this graph has no forum data", and returned nothing. I never found what removed the index. I
recreated it and added a test that every index the search depends on exists. The runtime still treats that error
as "no threads", so the test is what catches it now, not the app.

**Anonymous traffic could grow the graph, and some lookups scanned every node.** Each answer writes a small
`(:Session)-[:ASKED]->(:Query)` record so the next answer can say how it connects to what you asked before. That
is a write path for anyone, so requests are now rate-limited per client and sessions are capped. `GRAPH.EXPLAIN`
also showed three lookups scanning every node of their label, which three indexes fixed. Under 40 concurrent
requests, p95 latency was 5.8 seconds; the reranker is CPU-bound and this is a single process.

## Results, including where it loses

98 questions with known answers. The "has this been decided?" set has a held-out part I wrote after the system was
built and never tuned on. There are two baselines over exactly the same text: vector search with the same
embedding model, and BM25 keyword search. Both are scored on whether their top results contain the answer, which
is a ceiling on any model answering from them, and both get a no-match cutoff tuned on these very questions.

| Question | n | Lineage | Vector search | BM25 keyword search |
|---|---|---|---|---|
| Held-out ideas: right PEP as the top answer | 17 | 13 | 9 | 11 |
| Held-out ideas with no PEP: says "not recorded" | 5 | 5 | 5 | 5 |
| Right PEP found (Lineage: top answer; baselines: anywhere in top 5) | 37 | 29 | 36 | 35 |
| Is it still current? | 31 | 31 | 29 | 29 |
| Who decided it? | 6 | 5 | 1 | 1 |
| Did the idea come back? | 3 | 3 | 0 | 0 |
| How are two PEPs related? | 4 | 4 | 1 | 2 |

One result I didn't expect: plain keyword search beats vector search on the held-out ideas (11 against 9).
People proposing ideas tend to use the same words as the PEP that rejected them.

Read the bottom three rows carefully. The baselines can't represent those questions: a list of text chunks has no
way to say "this PEP cited that one", so their low scores show what retrieval can't express, not that Lineage is
accurate. The row that measures accuracy is the held-out one, and with 17 questions the gap could be luck. I did
not test an LLM reading the retrieved PEPs, which is the obvious stronger baseline.

Retrieval is better at one thing: getting the right PEP somewhere into its top five. Lineage is better at ranking
it first and at the follow-up questions. It is slower (0.64 s median against 0.03 s). And it is wrong in
ways I can name. Of the 32 answers where it was sure it had found the same idea, 3 cited the wrong PEP; the worst
is "Was there a PEP for function decorators?", which matched a rejected idea inside PEP 647 instead of PEP 318,
something `grep decorator` would have found.

Some of the questions in this post are themselves misses in the eval, phrased differently. "Has anyone proposed
keyword arguments in indexing, like a[x=1]?" finds PEP 472 and PEP 637 but only as "related". "Who decided PEP
3103?" doesn't name Guido. "Was a ternary conditional operator ever proposed?" still returns nothing despite the
abstract fix. Every miss is listed in `eval/RESULTS.md`.

## Keeping it current

The graph is never patched in place. `make sync` rebuilds it from source next to the live one, diffs the two into `(:Change)` nodes (new PEPs, status changes, rulings), copies session memory across,
and then switches an app-level alias key that every request resolves before querying. While four threads queried
throughout a two-minute sync, all 22,468 reads saw a complete graph and none failed.

## What I'd do next

- **Ideas that came back as new PEPs after 2019.** What changed between the attempts that made the second one land
  (PEP 3103 → PEP 634) or not (PEP 472 → PEP 637)?
- **Check the edge rule properly.** Label the 218 citations by hand and measure how often status and date get the
  relationship wrong.
- **A stronger baseline and more questions.** An LLM reading the top retrieved PEPs, and a held-out set big enough
  that the gaps mean something.

## Built with

Python 3.12, FalkorDB 4.22 (graph, vector and full-text indexes, `algo.pageRank`, `algo.WCC`, `algo.SPpaths`),
FastAPI, and fastembed for local embeddings and reranking. An MCP server (the protocol AI assistants use to call
tools) gives any agent the same 12 tools. No API key is needed; any OpenAI-compatible model can write the answers
instead of the templates, behind the guard.

Run it in a few minutes: `docker compose -f docker-compose.quick.yml up --build`, then open http://localhost:8000.
