# Demo video

[`docs/video/lineage-demo.mp4`](https://cdn.jsdelivr.net/gh/ANMOLGUPTA-24/lineage-agent@daa906ec312cfeb6ffd2708765a7150794c6dfe7/docs/video/lineage-demo.mp4): 2:50, 1080p, narrated (Kokoro-82M, local), captions burned in
(also `docs/video/lineage-demo.srt`). Recorded from the real UI against the live graph; rebuild with
`scripts/video/` (voice.py, record.py, build.py).

## Script as narrated

**hook** (home): Between 2019 and 2026, new lambda syntax was proposed on Python's Ideas forum eight times. The history was already there, spread across 745 PEPs, over a thousand rejected ideas, and over 1,700 forum threads. Lineage finds it.

**what** (home-hero): Lineage is a company brain for the Python project, built on FalkorDB. Paste an idea, and it answers from a graph: who proposed it, what was ruled and by whom, every time it came back, and who to ask.

**hero** (hero-timeline): Null coalescing. PEP 505 proposed it in 2015, and it was deferred. It came back as PEP 824 in 2026, and in two forum threads, with the people who took part. Documents, chats and people, in one graph.

**switch** (ask-switch): Should Python add a switch statement? Lineage finds PEP 3103, rejected in 2006, and quotes Guido's rejection notice, linked to the exact section. Then it follows the graph. PEP 622 took the idea up again, and was replaced by PEP 634, the match statement Python has today.

**chain** (ask-kwindex): Some ideas keep coming back. Keyword arguments in indexing were rejected in 2014, proposed again in 2020, and rejected by the Steering Council. Later typing PEPs cite that rejection. Lineage walks those links, and drafts a forum reply built only from what it found.

**explain** (explain): Every answer shows its work: the subgraph the queries walked, each index search, what the cross-encoder kept, and the exact Cypher that ran, with its parameters.

**how** (slide-how): FalkorDB holds PEPs, rejected ideas, rulings, pull requests, forum threads and people. Each rejected idea is its own node, linked to the earlier PEP it set aside. Vector and full-text indexes find candidates, a local cross-encoder checks they are the same idea, and Cypher traversals do the rest. PageRank finds who to ask. The same tools run as an MCP server, so any agent can use them.

**trust** (slide-results): No language model is required. When one writes the answer, a guard rejects any PEP number, name, verdict or quote the graph didn't return. On 98 questions, against vector search over the same data: who decided, 5 of 6, against 1 of 6. Did the idea come back: 3 of 3, against none. Lineage is wrong on 3 of 32 confident matches, and every miss is published.

**close** (slide-end): Lineage. Has Python already decided this? Try the live demo, or run it with Docker in about a minute. The links are below.

## Earlier recording plan (kept for reference)


`docs/video/lineage-demo.mp4` (2:28) is a captioned, silent walkthrough built from screenshots of the
running app, with the same story as below. It can be submitted as is. For a narrated version, record
the screen while following this script.

Record at 1080p, browser zoom 125%, dark or light theme (both are styled). Have the live Space and a
terminal open. Every answer below was produced by the current build; re-run each one right before
recording and use what it says, not this script, if they differ.

## 0:00–0:15 · The problem (talking head or title card)

> Python's Ideas forum keeps getting proposals Python already decided: do-while loops, switch
> statements, `?.` operators. Answering means digging through 25 years of PEPs and rulings.
> Lineage answers in about a second, quotes the ruling, and shows the graph path it walked.

## 0:15–0:45 · "Has anyone proposed a do-while loop?"

1. Type it. Answer: PEP 315, *rejected*, with the PEP's own Notice quoted:
   "no syntax emerged that could compete with … while True: … if not <condition>: break".
2. Point at the link: it opens `peps.python.org/pep-0315/#notice`, the exact section.
3. Open the trace panel: the tool (`already_decided`), the Cypher that ran, its parameters.
   Say: "The model never writes Cypher. It picks a tool; every query is hand-written and read-only."

## 0:45–1:15 · An idea that keeps coming back

1. Ask: "Has anyone proposed statement-local name bindings with a given clause?"
2. Answer: PEP 3150 (2010, deferred). It came up again: PEP 403 and PEP 572 (2018) set it aside.
3. In the graph panel, show `PEP 572 -[REJECTS_APPROACH_OF]-> PEP 3150` and the quote from 572's
   *Alternative spellings*: "See PEP 3150 for prior discussion on this subject …".
4. Say: "These edges come from the PEPs' own rejected-ideas sections: 50 of them, each with the
   sentence that set the other PEP aside. A vector store can find PEP 3150; it can't tell you it was
   rejected again eight years later."

## 1:15–1:35 · Getting work done: draft the reply

1. Ask: "Draft a reply to someone proposing a switch statement."
2. Show the draft: PEP 3103's Rejection Notice ("A quick poll during my keynote presentation at PyCon
   2007 shows this proposal has no popular support. I therefore reject it."), then PEP 622 → 634,
   the pattern matching that came later. Links throughout; who to ask at the end.
3. Say: "Only matches the cross-encoder calls the same idea go into a draft. Nothing is posted."

## 1:35–2:00 · Graph algorithms doing real work

1. "Who should I ask about typing?" → PageRank over a per-area influence graph: Guido, Łukasz
   Langa, Jukka Lehtosalo, **Mark Shannon** (he decided PEP 484, and decider edges carry
   authority). "Most active" is listed separately: Jelle Zijlstra, 79 PEPs and PRs.
2. "What is the bus factor of the cluster around PEP 3115?" → WCC with and without the top
   person: the largest connected group drops from 13 to 9, cutting off 3 people.
3. "How is PEP 484 related to PEP 695?" → one hop, a direct citation. Say: "Before we made
   prolific authors cost more, the shortest path was 'Guido wrote both'."

## 2:00–2:15 · Honesty

1. "Has anyone proposed a banana keyword?" → "No PEP, rejected idea or discuss.python.org thread in
   the graph matches … That is not proof the idea is new."
2. Say: "If the model's answer names a PEP, a person or a quote the tools didn't return, a guard
   replaces it with an answer built from the rows."

## 2:15–2:40 · Evidence (show eval/RESULTS.md or the README table)

- Questions answered by a relationship: who decided (83% vs 17%), did the idea come back (100% vs
  0%), how two PEPs relate (100% vs 25%), against a vector baseline given the same corpus.
- Held-out ideas: right PEP as the top answer 76% vs 53%.
- Say the weakness out loud: "3 of 32 confident matches were wrong; they're listed in the repo."

## 2:40–2:55 · Keeping current

- Terminal: `make sync`. Say: "It rebuilds beside the live graph, diffs it into Change events and
  flips an alias in one write. During a sync, 22,468 concurrent reads all saw a complete graph."
- Ask "What changed recently?" in the UI.

## 2:55–3:00 · Close

> Lineage. 745 PEPs, 25 years of decisions, every answer with the path that produced it.
> github.com/ANMOLGUPTA-24/lineage-agent
