# Demo video

[`docs/video/lineage-demo.mp4`](https://cdn.jsdelivr.net/gh/ANMOLGUPTA-24/lineage-agent@daa906ec312cfeb6ffd2708765a7150794c6dfe7/docs/video/lineage-demo.mp4): 2:51, 1080p, narrated (Kokoro-82M, local), captions burned in
(also `docs/video/lineage-demo.srt`). Recorded from the real UI against the live graph; rebuild with
`scripts/video/` (voice.py, record.py, build.py).

## Script as narrated

**hook** (home): Between 2019 and 2026, new lambda syntax was proposed on Python's Ideas forum eight times. The history was already there, spread across 745 PEPs, over a thousand rejected ideas, and over 1,700 forum threads. Lineage finds it.

**what** (home-hero): Lineage is a company brain for the Python project, built on FalkorDB. Paste an idea, and it answers from a graph: who proposed it, what was ruled and by whom, every time it came back, and who to ask.

**hero** (hero-timeline): Null coalescing. PEP 505 proposed it in 2015, and it was deferred. It came back as PEP 824 in 2026, and in two forum threads, with the people who took part. Documents, chats and people, in one graph.

**switch** (ask-switch): Should Python add a switch statement? Lineage finds PEP 3103, proposed in 2006 and rejected a year later, and quotes Guido's rejection notice, linked to the exact section. Then it follows the graph. PEP 622 took the idea up again, and was replaced by PEP 634, the match statement Python has today.

**chain** (ask-kwindex): Some ideas keep coming back. Keyword arguments in indexing were rejected in 2014, proposed again in 2020, and rejected by the Steering Council. Later typing PEPs cite that rejection. Lineage walks those links, and drafts a forum reply built only from what it found.

**explain** (explain): Every answer shows its work: the subgraph the queries walked, each index search, what the cross-encoder kept, and the exact Cypher that ran, with its parameters.

**how** (slide-how): FalkorDB holds PEPs, rejected ideas, rulings, pull requests, forum threads and people. Each rejected idea is its own node, linked to the earlier PEP it set aside. Vector and full-text indexes find candidates, a local cross-encoder checks they are the same idea, and Cypher traversals do the rest. PageRank finds who to ask. The same tools run as an MCP server, so any agent can use them.

**trust** (slide-results): No language model is required. When one writes the answer, a guard rejects any PEP number, name, verdict or quote the graph didn't return. On 98 questions, against vector search over the same data: who decided, 5 of 6, against 1 of 6. Did the idea come back: 3 of 3, against none. Lineage is wrong on 3 of 32 confident matches, and every miss is published.

**close** (slide-end): Lineage. Has Python already decided this? Try the live demo, or run it with Docker in about a minute. The links are below.
