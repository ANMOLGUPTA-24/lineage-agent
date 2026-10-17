# Triage agent on recent Ideas topics

The triage policy (`lineage/triage.py`, `decide()` only: dry run, nothing written) over the 60 most recent Ideas topics in the cached forum data (newest 2026-10-16, oldest 2026-08-19). Each topic is decided as if just posted: its own thread and anything newer are excluded.

This measures **what the agent does**, not whether it is right. The evidence behind every decision that cites the record is listed below for a person to check. Method: `eval/triage_eval.py`.

## Actions

| Action | Topics |
|---|---|
| reply_with_ruling | 1 |
| point_to_existing | 0 |
| link_prior_threads | 0 |
| flag_for_review | 17 |
| route_to_experts | 42 |

Routed to at least one person: 48 of 60. Median decision time 1.69 s (max 2.25 s), models warm, one CPU process.

## reply_with_ruling (1)

| Posted | Topic | Evidence (same idea first) | Routed to |
|---|---|---|---|
| 2026-08-26 | [Define a concurrency model (aka memory model) for Python](https://discuss.python.org/t/108762) | same: [PEP 583: A Concurrency Memory Model for Python](https://peps.python.org/pep-0583/) | Jeffrey Yasskin |

## point_to_existing (0)

None.

## link_prior_threads (0)

None.

## Hand check (2026-10-17, 9 decisions, by reading the topic and evidence titles and links)

- reply_with_ruling, 1 of 1 checked: right. "Define a concurrency model (aka memory model)" is the idea of PEP 583,
  withdrawn in 2008.
- flag_for_review, 8 of 17 checked (the 8 newest, titles only): none should have been a ruling. One is arguably
  link_prior_threads (109407 "More of my ideas for Python 3.16", whose clear-screen idea was already in the
  earlier "My ideas for Python 3.16" thread); two have weak evidence (insertdict() vs a defaultlist thread,
  private-by-default imports vs PEP 690 lazy imports); the other five are fairly related history.
- route_to_experts was not hand-checked for missed matches. Spot checks during development found at least one
  (109406 "a helper for writing `__repr__`" has an earlier thread "New function `repr_args` in `pprint` or
  `reprlib`" that the cross-encoder scored -2.5, below the related cutoff of 1.0), so read 42 routings as
  "nothing confident found", not "new".
- The verdict quoted is a PEP's status today, not as of the topic's date; for topics from 2026 that is the same.
