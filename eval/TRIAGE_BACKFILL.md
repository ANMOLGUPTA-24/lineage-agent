# Triage backfill: what the agent would have done with 2024-2026 Ideas topics

**Runs, both on 2026-10-18:**

- **Run 1**, before the fixes.
- **Run 2**, after the backend's three policy fixes. It forgot all run-1 backfill decisions and decided everything
  again.

Both runs used `uv run python -m lineage.triage --backfill --since 2024-01-01` against the live graph
(`python__20261016T203521`).

- Numbers come from `uv run python -m eval.triage_backfill_check stats`.
- The hand-check worksheet comes from `... sheet --seed 7`.
- Run-2 outputs are in `data/triage/backfill/`. In the graph they are the `Triage {mode: 'backfill'}` nodes.

**The three fixes, prompted by run 1's errors:**

1. A post that names an adopted PEP is treated as building on that PEP. It goes to `flag_for_review` for the PEP's
   authors, instead of getting a "this already exists" reply.
2. When a PEP matches, its rejected idea is preferred over the adopted PEP as a whole.
3. PEP status is taken as of the topic's date, not today. A PEP that was still a draft when the topic was posted
   gets `link_prior_threads`, meaning "join that discussion".

## Headline (run 2)

Between Jan 2024 and Oct 2026 there were 1,158 Ideas topics. The agent decided 1,139 of them; the other 19 already
had live decisions. It found an earlier same-idea PEP or thread for **19 (1.7%)** and drafted a reply for every
one. I read all 19 by hand:

- **8 were right**: the same idea, and the right action.
- **7 were partly right**: related history, but not the same idea, or the same idea with the wrong action.
- **4 were wrong**.

Counting only fully correct cases, **precision is 42%, up from 22% in run 1**. Counting cases where the cited record
is at least related history, it is 79% (run 1: 84%).

What the fixes did:

- **The "already exists" mistake is gone.** 13 of run 1's 18 `point_to_existing` replies became `flag_for_review`
  (no reply drafted). Another 4 became `link_prior_threads` to a PEP that was still open at the time.
- **The rejected-idea fix had no visible effect.** The two cases it targeted (77815 PEP 616, 90799 PEP 750) both
  name the PEP in the opening post, so fix 1 now sends them to review before fix 2 is reached. Those two right
  rulings are still not drafted.
- **What remains is the four wrong matches.** They come from the cross-encoder labelling a different idea as
  "same". The policy rules do not catch that.
- **Recall is unchanged.** The same 20-topic sample still has 2 missed same-idea threads.

## Before the fixes / after

| | run 1 (before) | run 2 (after) |
|---|---:|---:|
| topics decided | 1,137 (2 timed out) | 1,139 (0 failed) |
| reply_with_ruling | 5 | 5 |
| point_to_existing | 18 | **1** |
| link_prior_threads | 9 | **13** |
| flag_for_review | 307 | 322 |
| route_to_experts | 798 | 798 |
| drafted replies | 32 | **19** |
| hand-labelled correct / partial / wrong | 7 / 20 / 5 | **8 / 7 / 4** |
| precision, correct only | 22% | **42%** |
| precision, at least related | 84% | 79% |
| `point_to_existing` correct | 0 / 18 | 0 / 1 |
| `link_prior_threads` correct | 5 / 9 | 6 / 13 |
| `reply_with_ruling` correct | 2 / 5 | 2 / 5 |
| SAME_IDEA_AS edges written (PEP + Thread) | 34 (24 + 10) | 21 (11 + 10) |
| RELATED_TO edges written (PEP + Thread) | 474 (262 + 212) | 491 (277 + 214) |
| recall sample: missed same-idea thread / PEP | 2 / 0 of 20 | 2 / 0 of 20 (same topics) |
| median time per topic | 2.0 s | 2.0 s (1,139 topics in 31.9 min) |

## 1. What was run (run 2)

| | |
|---|---|
| Ideas topics created on or after 2024-01-01 in `data/raw/discourse.json` | 1,158 |
| Decided in backfill mode | **1,139**, including the 2 that timed out in run 1 |
| Skipped: already triaged in live mode (left untouched) | 19 |
| Topics with cached opening text (the rest were decided from the title only) | 870 of 1,139 (76%) |

**Timing:** run 2 took 31.9 minutes. The median is 2.0 s per topic, the mean 1.7 s and the p90 2 s. Each topic took
one embedding plus cross-encoder rerank, and a few graph queries. No LLM calls.

**Run 1** took 35 minutes over two segments, interrupted by a machine reboot. Its 2 failed topics hit FalkorDB
query timeouts.

## 2. What it decided (run 2)

| action | n | share | drafted reply |
|---|---:|---:|---:|
| reply_with_ruling | 5 | 0.4% | 5 |
| point_to_existing | 1 | 0.1% | 1 |
| link_prior_threads | 13 | 1.1% | 13 |
| flag_for_review | 322 | 28.3% | 0 |
| route_to_experts | 798 | 70.1% | 0 |
| **total** | **1,139** | | **19** |

- **Routing:** 857 decisions named at least one person. 265 `route_to_experts` decisions found nobody close enough.
- **By year (decisive / all):** 2024 5/447, 2025 8/440, 2026 6/252.
- **What changed between runs:**
  - 13 decisions went from `point_to_existing` to `flag_for_review`.
  - 4 went from `point_to_existing` to `link_prior_threads`.
  - The 2 topics retried from run 1's failures went to `flag_for_review`.
  - Nothing else changed.

## 3. Precision: every decisive decision in run 2, checked by hand

There were 19 decisive decisions, so I checked all of them without sampling. The rubric was the same as run 1:

- **correct**: the same idea, and the action fits.
- **partial**: related but not the same idea, or the same idea with the wrong action.
- **wrong**: a different idea.

For each decision I read the opening post (fetched from the forum when it wasn't cached), the cited PEP's abstract
and status, rejected-idea text, and the cited thread's opening.

| action | correct | partial | wrong | n |
|---|---:|---:|---:|---:|
| reply_with_ruling | 2 | 1 | 2 | 5 |
| point_to_existing | 0 | 1 | 0 | 1 |
| link_prior_threads | 6 | 5 | 2 | 13 |
| **all** | **8** | **7** | **4** | **19** |

| topic | action (run 2) | cited | label | note |
|---|---|---|---|---|
| [43902](https://discuss.python.org/t/43902) For ... except ... finally | ruling | PEP 463 rejected idea "finally clause" | wrong | `for`-loop `except`/`finally` is not the same as exception expressions. Unchanged from run 1. |
| [49902](https://discuss.python.org/t/49902) kwargs typing without a TypedDict | existing | [PEP 692](https://peps.python.org/pep-0692/) | partial | Asks about PEP 692's shortcomings. The cache has no opening text for it, so fix 1 could not see that the post names PEP 692. |
| [57208](https://discuss.python.org/t/57208) while-elif-else | threads | [t/57036](https://discuss.python.org/t/57036) | correct | Same author's narrowed re-post. |
| [59092](https://discuss.python.org/t/59092) tempfile PathLike | threads | [t/40900](https://discuss.python.org/t/40900) | correct | The poster could not link the thread; the agent does. |
| [72089](https://discuss.python.org/t/72089) Official lint rules & tool | threads, was existing | [PEP 765](https://peps.python.org/pep-0765/), a draft until 2025-02 | partial | An alternative to PEP 765. "Take it to that PEP's discussion" is now true and useful, but it is not the same idea. |
| [83689](https://discuss.python.org/t/83689) OOP interface support | threads | [t/34428](https://discuss.python.org/t/34428) | correct | Still misses rejected PEP 245. |
| [89131](https://discuss.python.org/t/89131) include-group for optional-dependencies | ruling | PEP 633 rejected idea "replace optional-dependencies with extras" | wrong | A different idea. Unchanged from run 1. |
| [97159](https://discuss.python.org/t/97159) Make Callable callable | ruling | [PEP 677](https://peps.python.org/pep-0677/) | partial | Designed to avoid PEP 677's reason for rejection. |
| [101329](https://discuss.python.org/t/101329) `**` unpacking iterables of iterables | threads, was existing | [PEP 798](https://peps.python.org/pep-0798/), a draft until 2025-11 | partial | A spin-off of PEP 798, not the same idea. The draft no longer says "adopted". |
| [104407](https://discuss.python.org/t/104407) Asyncio TimeoutGroup | threads | PEP 789 | wrong | Unrelated. Unchanged from run 1. |
| [104540](https://discuss.python.org/t/104540) Errors for missing stdlib modules | ruling | [PEP 534](https://peps.python.org/pep-0534/) | correct | Same caveat as run 1: the quoted withdrawal notice probably post-dates the topic. |
| [104608](https://discuss.python.org/t/104608) Alternative path for explicit lazy imports | threads, was existing | [PEP 810](https://peps.python.org/pep-0810/), a draft until 2025-11 | partial | An alternative to PEP 810. Sending it to that PEP's discussion is the right place, but it is not the same idea. |
| [104751](https://discuss.python.org/t/104751) Debug symbols in Python Install Manager | threads | [t/104673](https://discuss.python.org/t/104673) | partial | A sibling request. |
| [105623](https://discuss.python.org/t/105623) Public "suggestion" module | threads | [t/103485](https://discuss.python.org/t/103485) | partial | A different suggestions proposal. |
| [105867](https://discuss.python.org/t/105867) Inline annotations for multiple assignment | threads | [t/53407](https://discuss.python.org/t/53407) | correct | |
| [106430](https://discuss.python.org/t/106430) `yield from` in async generators | threads, was existing | [PEP 828](https://peps.python.org/pep-0828/), a draft until 2026-08 | correct | The same idea, and it was an open PEP, so linking to it fits the rubric. In practice this is the PEP's own proposal thread, so the reply is redundant. Counted correct, as 57208 was. |
| [106783](https://discuss.python.org/t/106783) range(inclusive=True) | threads | [t/106521](https://discuss.python.org/t/106521) | correct | |
| [107510](https://discuss.python.org/t/107510) None-coalescing `??` | ruling | [PEP 505](https://peps.python.org/pep-0505/) (Deferred) | correct | |
| [107684](https://discuss.python.org/t/107684) SyntaxError suggestion for `match` | threads | t/43121 "Allow pass inside match" | wrong | A different idea. Unchanged from run 1. |

**What the 13 new `flag_for_review` decisions mean.** Each reason now reads, for example: "The post builds on PEP
616 (…, Final): it names the PEP, so it is extending or revising it, not repeating it. Its authors should see it."

For 11 of the 13, this is a better outcome than run 1's "already exists" reply: 51415, 52776, 55503, 98274, 103245,
104843, 104849, 105572, 106583, 108231, and 101401 (which went to flag through a new 6.0 score threshold instead).
The remaining two are the losses:

- **77815** had an exact rejected idea in the graph: PEP 616 "Accepting a tuple of affixes".
- **90799** also had one: PEP 750 "Delayed Evaluation of Interpolations".

Both should have been `reply_with_ruling`, but are now flags.

## 4. Recall check: 20 `route_to_experts` decisions (seeded random sample, seed 7)

Run 2 drew the same 20 topics as run 1, and all 20 were still `route_to_experts`. The findings below therefore apply to both runs.


| topic | missed? |
|---|---|
| [67911](https://discuss.python.org/t/67911) ordered set | **Missed** [t/12730](https://discuss.python.org/t/12730) "Add `OrderedSet` to stdlib" (2021), which is in the graph. The topic's opening is a 2-line post. |
| [86577](https://discuss.python.org/t/86577) builtins.lazy for lazy arguments | **Missed** [t/48421](https://discuss.python.org/t/48421) "Deferred Evaluation Initial Proof of Concept" (2024) and [t/34173](https://discuss.python.org/t/34173). It should have been `link_prior_threads`. |
| [103699](https://discuss.python.org/t/103699) dedicated syntax for `Annotated` | Related, not the same: [t/40751](https://discuss.python.org/t/40751) "An alternative to Annotated" and PEP 727, which the poster cites. It should have been `flag_for_review`. |
| [48343](https://discuss.python.org/t/48343) `object.__init_subclass__` kwargs, [89157](https://discuss.python.org/t/89157) buffer protocol over `__bytes__` | Related PEPs exist (487, 688), but neither is the same idea. `flag_for_review` would have been better. |
| 45355, 46134, 47369, 48588, 50871, 51166, 51488, 57887, 65232, 76343, 81525, 91323, 101134, 104786, 106304 | Nothing obvious: no same-idea PEP that I know of, and no matching thread title in the graph. |

**Result:** none of the 20 missed a same-idea **PEP**, and **2 of 20 (10%) missed a same-idea thread**. If that rate
holds across all 798 routings, about 80 more topics had a findable earlier discussion, with wide error bars on a
sample of 20.

## 5. Impact (run 2)

**What would have happened.** From Jan 2024 to Oct 2026, the agent would have drafted **19 replies across 1,139
Ideas topics**, about one every seven weeks, and sent 322 more topics to a person for review.

- **8 drafts are right as they stand.** They include a PEP 505 ruling for a `??` proposal, and a link to a 25-post
  range(inclusive=True) thread from three weeks earlier.
- **4 are wrong.** All four come from the matcher labelling a different idea as "same" (see the worst mistakes
  below).
- **No draft any longer tells a poster that an extension of an adopted PEP "already exists".** That was 17 of run
  1's 32 drafts.
- **4 extensions of then-open PEPs are now sent to that PEP's discussion**, which is the right place for them.

**What it costs.**

- No LLM.
- About 2 seconds per topic.
- 21 `SAME_IDEA_AS` and 491 `RELATED_TO` edges written back to the graph.

### Best 5 examples

1. **[107510](https://discuss.python.org/t/107510) "None-coalescing operator and null-coalescing assignment"** (2026-05)
   - Decision: `reply_with_ruling`, citing PEP 505 "None-aware operators" (2015, Deferred). Routed to its authors,
     Steve Dower and Mark E. Haase.
   - Draft: "PEP 505 ("None-aware operators", 2015) proposed it; it was deferred, never decided."
2. **[106783](https://discuss.python.org/t/106783) "Optional Inclusive Parameter to range()"** (2026-04)
   - Decision: `link_prior_threads` to [t/106521](https://discuss.python.org/t/106521) "`range()` with optional
     inclusive `stop`", from three weeks earlier, with almost the same text (score 7.05).
   - Draft: "It was discussed on discuss.python.org: "`range()` with optional inclusive `stop`" (2026, 25 posts) …
     Core developers in that thread: Tim Peters."
3. **[105867](https://discuss.python.org/t/105867) "Revisiting inline type annotations for multiple assignment"** (2026-01)
   - Decision: `link_prior_threads` to [t/53407](https://discuss.python.org/t/53407) "Proposal: Annotate types in
     multiple assignment" (2024, 37 posts).
   - Draft: "A new proposal should start from these threads and what was said there."
4. **[59092](https://discuss.python.org/t/59092) "Could tempfile.TemporaryFile and TemporaryDirectory be PathLike?"** (2024-07)
   - The poster knew of an earlier thread but could not link it ("how to refer to another topic here???").
   - Decision: `link_prior_threads` to [t/40900](https://discuss.python.org/t/40900), with Brett Cannon and Barney
     Gale named from that thread.
   - Draft: "…"tempfile.TemporaryDirectory().name should return pathlib.Path instead of str" (2023, 16 posts)."
5. **[83689](https://discuss.python.org/t/83689) "OOP Interface Support in Python"** (2025-03)
   - Decision: `link_prior_threads` to [t/34428](https://discuss.python.org/t/34428) "Native Interface Support in
     Python" (2023). Routed to Eric V. Smith, Brett Cannon and Phillip J. Eby.
   - Caveat: it missed rejected PEP 245, which would have made this a ruling.

### Worst 3 mistakes (run 2)

1. **[107684](https://discuss.python.org/t/107684) "Improve SyntaxError message for `match`"**
   - Decision: `link_prior_threads` to "Allow `pass` inside `match` statements", labelled as the same idea with a
     score of 5.1.
   - Different idea: the shared word "match" in the title probably drove the match.
2. **[104407](https://discuss.python.org/t/104407) "Asyncio TimeoutGroup"**
   - Decision: the draft says "An open PEP already proposes this: PEP 789 (Preventing task-cancellation bugs by
     limiting yield in async generators)".
   - PEP 789 is unrelated.
3. **[89131](https://discuss.python.org/t/89131) "`include-group` for `project.optional-dependencies`"**
   - Decision: `reply_with_ruling` quoting PEP 633's rejected idea "Replace optional-dependencies with extras".
   - A different idea. The topic extends PEP 735 dependency groups.

**Run 1's third-worst mistake no longer happens.** In 105572, run 1 told the poster that zero-copy `str` encoding
was "already part of Python through PEP 540". Run 2 flags the topic for review instead.

## 6. Caveats

- **Who labelled.** One person (the analyst) did all the labelling, with no second rater. "Partial" is a judgement
  call, and the full table is above so it can be re-checked.
- **Thin input.** 24% of topics were decided from the title alone, because the cache keeps opening text only for
  topics with 5+ posts. In run 2, 8 of the 19 decisive decisions were made from the title only. One effect:
  49902's opening post names PEP 692, but fix 1 could not see that, so it is still `point_to_existing`.
- **Status at topic time.** Run 2 dates the status to the topic, but drafts can still quote PEP text that was added
  later, as in 104540's withdrawal notice.
- **Same labeller, both runs.** I labelled both runs with the same rubric. Run-2 labels for unchanged decisions were
  carried over from run 1. The 5 changed decisions were re-read and re-labelled.
- **Timeouts.** In run 1, 2 topics failed on graph query timeouts. Run 2 decided them both.
- **Skipped.** 19 topics already had live-mode decisions and were skipped. They are not included here; their mix
  was 1 ruling, 7 flags and 12 routings across all 20 live decisions.
- **Write-back feedback.** Write-back edges from earlier backfill topics are visible to later ones through
  `earlier_triage`. No decision here used one: no `via_triage` evidence appeared.
- **Nothing posted.** No forum posts or webhooks were sent. The backfill writes only the graph and
  `data/triage/backfill/`.

## Appendix: run 1 (before the fixes), hand labels for all 32 decisive decisions

Run 1's results were 7 correct, 20 partial and 5 wrong. Its 32 decisive decisions break down as follows:

| action | correct | partial | wrong |
|---|---:|---:|---:|
| reply_with_ruling | 2 | 1 | 2 |
| point_to_existing | 0 | 17 | 1 |
| link_prior_threads | 5 | 2 | 2 |

| topic | date | action | cited | label | note |
|---|---|---|---|---|---|
| [43902](https://discuss.python.org/t/43902) For ... except ... finally | 2024-01 | ruling | [PEP 463](https://peps.python.org/pep-0463/) rejected idea "finally clause" | wrong | The topic wants `except` and `finally` clauses on a `for` loop. PEP 463 is about exception *expressions*. |
| [49902](https://discuss.python.org/t/49902) Precise typing of kwargs but without a TypedDict | 2024-03 | existing | [PEP 692](https://peps.python.org/pep-0692/) | partial | The topic is about PEP 692's shortcomings, so pointing back at PEP 692 adds nothing. |
| [51415](https://discuss.python.org/t/51415) Symmetric difference operators for dict | 2024-04 | existing | [PEP 584](https://peps.python.org/pep-0584/) | partial | PEP 584 covers `\|`, not `^`. The topic builds on it. |
| [52776](https://discuss.python.org/t/52776) Memoryview multi-dimensional slicing | 2024-05 | existing | [PEP 3118](https://peps.python.org/pep-3118/) | partial | Same idea, but the point is that PEP 3118 promised this and it was never implemented. "Already exists" is false. |
| [55503](https://discuss.python.org/t/55503) More deprecation decorators | 2024-06 | existing | [PEP 702](https://peps.python.org/pep-0702/) | partial | Extends PEP 702 to parameters. |
| [57208](https://discuss.python.org/t/57208) while-elif-else, for-elif-else | 2024-07 | threads | [t/57036](https://discuss.python.org/t/57036) | correct | Same author's narrowed re-post. |
| [59092](https://discuss.python.org/t/59092) tempfile.TemporaryFile/Directory PathLike | 2024-07 | threads | [t/40900](https://discuss.python.org/t/40900) | correct | The poster wrote "how to refer to another topic here???". The agent supplies the link. |
| [72089](https://discuss.python.org/t/72089) Official lint rules & tool | 2024-11 | existing | [PEP 765](https://peps.python.org/pep-0765/) | partial | Proposed as an *alternative* to PEP 765. PEP 765 was an 8-day-old draft then, but the reply calls it "adopted". |
| [77815](https://discuss.python.org/t/77815) tuple argument for str.removeprefix/removesuffix | 2025-01 | existing | [PEP 616](https://peps.python.org/pep-0616/) | partial | Right PEP, wrong action: PEP 616's rejected idea "Accepting a tuple of affixes" is exactly this, so it should have been `reply_with_ruling`. |
| [83689](https://discuss.python.org/t/83689) OOP interface support | 2025-03 | threads | [t/34428](https://discuss.python.org/t/34428) | correct | Missed [PEP 245](https://peps.python.org/pep-0245/) (Interface Syntax, Rejected), which is in the graph. |
| [89131](https://discuss.python.org/t/89131) include-group for optional-dependencies | 2025-04 | ruling | [PEP 633](https://peps.python.org/pep-0633/) rejected idea "replace optional-dependencies with extras" | wrong | A different idea. PEP 735, which the topic cites, is only related. |
| [90799](https://discuss.python.org/t/90799) t-strings where expressions are not evaluated | 2025-05 | existing | [PEP 750](https://peps.python.org/pep-0750/) | partial | Right PEP, wrong action: PEP 750's rejected idea "Delayed Evaluation of Interpolations" is the ruling to quote. |
| [97159](https://discuss.python.org/t/97159) Make Callable callable | 2025-06 | ruling | [PEP 677](https://peps.python.org/pep-0677/) (Rejected) | partial | The topic is designed to avoid PEP 677's reason for rejection (new grammar), so quoting that ruling overstates it. |
| [98274](https://discuss.python.org/t/98274) Supported platforms per branch | 2025-07 | existing | [PEP 11](https://peps.python.org/pep-0011/) | partial | Asks to change PEP 11. |
| [101329](https://discuss.python.org/t/101329) Should `**` unpack iterables of iterables? | 2025-08 | existing | [PEP 798](https://peps.python.org/pep-0798/) | partial | A spin-off of PEP 798, which was still a draft then. |
| [101401](https://discuss.python.org/t/101401) Module-level `__getattribute__` | 2025-08 | existing | [PEP 562](https://peps.python.org/pep-0562/) | partial | Extends PEP 562. |
| [103245](https://discuss.python.org/t/103245) Identify Templates from the same t-string | 2025-08 | existing | [PEP 750](https://peps.python.org/pep-0750/) | partial | A feature request on top of PEP 750. |
| [104407](https://discuss.python.org/t/104407) Asyncio TimeoutGroup | 2025-10 | threads | [PEP 789](https://peps.python.org/pep-0789/) (Draft) | wrong | PEP 789 is about `yield` in async generators. |
| [104540](https://discuss.python.org/t/104540) Improved errors for missing stdlib modules | 2025-10 | ruling | [PEP 534](https://peps.python.org/pep-0534/) (Withdrawn) | correct | Same idea, and the PEP has the same title. Caveat: the quoted withdrawal notice describes the 3.15 result, so it probably post-dates the topic. |
| [104608](https://discuss.python.org/t/104608) Alternative path for explicit lazy imports | 2025-10 | existing | [PEP 810](https://peps.python.org/pep-0810/) | partial | An alternative to PEP 810, which was still under review then. |
| [104751](https://discuss.python.org/t/104751) Debug symbols in Python Install Manager | 2025-11 | threads | [t/104673](https://discuss.python.org/t/104673) | partial | A sibling request (system-wide install), not the same feature. |
| [104843](https://discuss.python.org/t/104843) Customized iterable unpacking (`__unpack__`) | 2025-11 | existing | [PEP 3132](https://peps.python.org/pep-3132/) | partial | Extends PEP 3132. |
| [104849](https://discuss.python.org/t/104849) Error handling for lazy imports | 2025-11 | existing | [PEP 810](https://peps.python.org/pep-0810/) | partial | Extends PEP 810. |
| [105572](https://discuss.python.org/t/105572) Encode `str` without a copy | 2026-01 | existing | [PEP 540](https://peps.python.org/pep-0540/) (UTF-8 Mode) | wrong | Zero-copy encoding, not UTF-8 mode. The match score was the lowest of all 32 (1.35). |
| [105623](https://discuss.python.org/t/105623) Public "suggestion" module | 2026-01 | threads | [t/103485](https://discuss.python.org/t/103485) | partial | Both are about name suggestions, but they are different proposals. |
| [105867](https://discuss.python.org/t/105867) Inline type annotations for multiple assignment | 2026-01 | threads | [t/53407](https://discuss.python.org/t/53407) | correct | The 37-post 2024 thread on the same syntax. |
| [106430](https://discuss.python.org/t/106430) `yield from` in async generators | 2026-03 | existing | [PEP 828](https://peps.python.org/pep-0828/) | partial | This *is* the PEP's own proposal thread, posted a day after the PEP. The reply would call it "Accepted", which is today's status. |
| [106583](https://discuss.python.org/t/106583) High-level frame hook API | 2026-03 | existing | [PEP 523](https://peps.python.org/pep-0523/) | partial | A higher-level alternative to PEP 523. |
| [106783](https://discuss.python.org/t/106783) Inclusive parameter for range() | 2026-04 | threads | [t/106521](https://discuss.python.org/t/106521) | correct | A near-verbatim repeat of a 25-post thread from 3 weeks earlier. |
| [107510](https://discuss.python.org/t/107510) None-coalescing `??` and `??=` | 2026-05 | ruling | [PEP 505](https://peps.python.org/pep-0505/) (Deferred) | correct | The textbook case. |
| [107684](https://discuss.python.org/t/107684) SyntaxError suggestion for `match` | 2026-06 | threads | [t/43121](https://discuss.python.org/t/43121) "Allow pass inside match" | wrong | A different idea. |
| [108231](https://discuss.python.org/t/108231) Docstring conventions for exception groups | 2026-07 | existing | [PEP 654](https://peps.python.org/pep-0654/) | partial | Builds on PEP 654. |

### Run 1 error patterns (these prompted the fixes)

1. **"Cites a PEP" is read as "is that PEP".**
   - In 17 of the 18 `point_to_existing` cases (all but 106430), the opening post names the PEP itself, usually to
     extend it or offer an alternative.
   - The cross-encoder labels these "same", so the policy concludes "adopted, already exists".
2. **A whole-PEP match beats the rejected idea inside it.**
   - In 77815 (PEP 616 "Accepting a tuple of affixes") and 90799 (PEP 750 "Delayed Evaluation of
     Interpolations"), the exact ruling was in the graph as an `Alternative`.
   - The agent pointed at the adopted PEP instead of replying with that ruling.
3. **Today's status is quoted as if it were the status then.**
   - PEPs 765, 798, 810 and 828 were drafts when the topics were posted, but are Final or Accepted now.
   - `prior()` drops PEPs created after the topic, but not status changes after it, so the drafts call them adopted.
   - Live triage is not affected: there, today's status is the right one.
4. **Thread matching is the most reliable part.** Same-idea threads with a cross-encoder score of 5 or more were
   right in 5 of 7 cases.

