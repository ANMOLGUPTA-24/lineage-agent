"""Keep the graph current: rebuild beside the live graph, record what changed, swap atomically.

  make sync              rebuild from data/raw and swap
  make sync FETCH=1      first pull peps.json and the python/peps repo (no GitHub token needed)
  make rollback          point the tenant back at the previous graph

1. Build a new physical graph `<tenant>__<UTC stamp>` with the normal ingest (LINEAGE_GRAPH
   points it there). The live graph keeps serving the whole time.
2. Check it: refuse to swap if it has no PEPs or lost more than 5% of the live graph's PEPs.
3. Diff it against the live graph and write (:Change {at, kind, detail})-[:CHANGED]->(:PEP) for
   new PEPs, status changes, new supersessions, new rulings, newly linked PRs / issues and newly
   recorded rejected approaches. Earlier Change / Sync history and agent memory (Finding, and
   Session -> Query -> PEP) are copied across.
4. SET lineage:active:<tenant> to the new graph: one atomic write. Queries already running
   finish on the old graph; new ones read the new one.
5. Copy Findings written to the old graph while 1-4 ran, keep the previous graph for rollback,
   delete older ones.

A Redis lock (SET NX with expiry) stops two syncs of the same tenant running at once.
"""
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from lineage.db import ALIAS, connect, resolve
from lineage.ingest.derive import try_query

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS = "lineage:previous:{}"
LOCK = "lineage:sync-lock:{}"
MAX_LOSS = 0.05


# ---------------------------------------------------------------- snapshot + diff

def snapshot(g) -> dict:
    q = lambda c: g.ro_query(c).result_set
    return {
        "peps": {n: (s, t) for n, s, t in q("MATCH (p:PEP) RETURN p.number, p.status, p.title")},
        "supersedes": {(a, b) for a, b in q("MATCH (a:PEP)-[:SUPERSEDES]->(b:PEP) RETURN a.number, b.number")},
        "rulings": {(n, o) for n, o in q("MATCH (p:PEP)-[:RESOLVED_BY]->(d:Decision) RETURN p.number, d.outcome")},
        "work": {(n, lbl, x) for n, lbl, x in q(
            "MATCH (x)-[:IMPLEMENTS|DISCUSSES]->(p:PEP) RETURN p.number, labels(x)[0], coalesce(x.number, x.id)")},
        "rejects": {(a, b) for a, b in q("MATCH (a:PEP)-[:REJECTS_APPROACH_OF]->(b:PEP) RETURN a.number, b.number")},
    }


def diff(old: dict, new: dict) -> list[dict]:
    """Changes from `old` to `new`, each attached to one PEP."""
    out = []
    for n, (status, title) in new["peps"].items():
        if n not in old["peps"]:
            out.append({"pep": n, "kind": "new_pep", "detail": f"PEP {n} ({title}) added as {status}"})
        elif old["peps"][n][0] != status:
            out.append({"pep": n, "kind": "status", "detail": f"PEP {n} status {old['peps'][n][0]} -> {status}"})
    for a, b in new["supersedes"] - old["supersedes"]:
        out.append({"pep": b, "kind": "superseded", "detail": f"PEP {b} is now superseded by PEP {a}"})
    for n, outcome in new["rulings"] - old["rulings"]:
        out.append({"pep": n, "kind": "ruling", "detail": f"PEP {n} has a new ruling: {outcome}"})
    for n, lbl, x in new["work"] - old["work"]:
        what = f"discuss.python.org thread {x}" if lbl == "Thread" else f"{lbl} #{x}"
        out.append({"pep": n, "kind": "work", "detail": f"{what} now linked to PEP {n}"})
    for a, b in new["rejects"] - old["rejects"]:
        out.append({"pep": b, "kind": "set_aside", "detail": f"PEP {a} set aside the approach of PEP {b}"})
    for n in old["peps"].keys() - new["peps"].keys():
        out.append({"pep": n, "kind": "removed", "detail": f"PEP {n} is no longer in the source data"})
    return sorted(out, key=lambda c: (c["pep"], c["kind"]))


# ---------------------------------------------------------------- carry-over

def copy_findings(src, dst, since: str = "") -> int:
    rows = src.ro_query("""MATCH (f:Finding)-[:ABOUT]->(p:PEP) WHERE f.at >= $since
                           RETURN f.text, f.at, f.session, p.number""", {"since": since}).result_set
    if rows:
        dst.query("""UNWIND $rows AS r MATCH (p:PEP {number: r[3]})
                     MERGE (f:Finding {text: r[0], at: r[1], session: r[2]})
                     MERGE (f)-[:ABOUT {weight: 1.0}]->(p)""", {"rows": [list(r) for r in rows]})
    return len(rows)


def copy_sessions(src, dst, since: str = "") -> int:
    """Session memory: (:Session)-[:ASKED]->(:Query)-[:ACCESSED]->(:PEP)."""
    rows = src.ro_query("""MATCH (s:Session)-[:ASKED]->(q:Query) WHERE q.at >= $since
                           OPTIONAL MATCH (q)-[:ACCESSED]->(p:PEP)
                           RETURN s.id, s.started, q.text, q.at, q.mode, collect(p.number)""", {"since": since}).result_set
    for i in range(0, len(rows), 300):
        dst.query("""UNWIND $rows AS r
                     MERGE (s:Session {id: r[0]}) ON CREATE SET s.started = r[1]
                     MERGE (s)-[:ASKED {weight: 1.0}]->(q:Query {text: r[2], at: r[3]}) SET q.mode = r[4]
                     WITH q, r UNWIND r[5] AS n MATCH (p:PEP {number: n})
                     MERGE (q)-[:ACCESSED {weight: 1.0}]->(p)""", {"rows": [list(r) for r in rows[i:i + 300]]})
    return len(rows)


def copy_history(src, dst) -> None:
    changes = src.ro_query("MATCH (c:Change)-[:CHANGED]->(p:PEP) RETURN c.at, c.kind, c.detail, p.number").result_set
    if changes:
        dst.query("""UNWIND $rows AS r MATCH (p:PEP {number: r[3]})
                     CREATE (:Change {at: r[0], kind: r[1], detail: r[2]})-[:CHANGED {weight: 1.0}]->(p)""",
                  {"rows": [list(r) for r in changes]})
    syncs = src.ro_query("MATCH (s:Sync) RETURN s.at, s.graph, s.previous, s.changes").result_set
    if syncs:
        dst.query("UNWIND $rows AS r CREATE (:Sync {at: r[0], graph: r[1], previous: r[2], changes: r[3]})",
                  {"rows": [list(r) for r in syncs]})


def write_changes(g, changes: list[dict], at: str) -> None:
    for i in range(0, len(changes), 500):
        g.query("""UNWIND $rows AS r MATCH (p:PEP {number: r.pep})
                   CREATE (:Change {at: $at, kind: r.kind, detail: r.detail})-[:CHANGED {weight: 1.0}]->(p)""",
                {"rows": changes[i:i + 500], "at": at})
    try_query(g, "CREATE INDEX FOR (c:Change) ON (c.at)")


# ---------------------------------------------------------------- the swap

def build(name: str) -> None:
    env = {**os.environ, "LINEAGE_GRAPH": name}
    subprocess.run(["make", "-C", str(ROOT), "ingest"], env=env, check=True)


def fetch() -> None:
    raw = ROOT / "data" / "raw"
    subprocess.run(["curl", "-sfL", "https://peps.python.org/api/peps.json", "-o", str(raw / "peps.json")], check=True)
    subprocess.run(["git", "-C", str(raw / "peps"), "pull", "--ff-only", "-q"], check=True)


def swap(tenant: str, new: str, build_fn=build) -> dict:
    db = connect()
    r = db.connection
    if not r.set(LOCK.format(tenant), new, nx=True, ex=3600):
        raise SystemExit(f"Another sync of {tenant!r} is running (lock {LOCK.format(tenant)}).")
    try:
        live_name = resolve(tenant)
        has_live = live_name in db.list_graphs()
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        build_fn(new)
        live, fresh = db.select_graph(live_name), db.select_graph(new)

        before = snapshot(live) if has_live else {"peps": {}, "supersedes": set(), "rulings": set(), "work": set(), "rejects": set()}
        after = snapshot(fresh)
        if not after["peps"] or len(after["peps"]) < (1 - MAX_LOSS) * len(before["peps"]):
            fresh.delete()
            raise SystemExit(f"Refusing to swap: new graph has {len(after['peps'])} PEPs, live has {len(before['peps'])}.")

        changes = diff(before, after) if has_live else []
        if has_live:
            copy_history(live, fresh)
            copy_findings(live, fresh)
            copy_sessions(live, fresh)
        write_changes(fresh, changes, started)
        fresh.query("CREATE (:Sync {at: $at, graph: $g, previous: $p, changes: $n})",
                    {"at": started, "g": new, "p": live_name if has_live else "", "n": len(changes)})

        r.set(ALIAS.format(tenant), new)                          # the swap: one atomic write
        late = (copy_findings(live, fresh, since=started) + copy_sessions(live, fresh, since=started)) if has_live else 0
        r.set(PREVIOUS.format(tenant), live_name if has_live else "")

        keep = {new, live_name}
        for name in db.list_graphs():
            if name.startswith(f"{tenant}__") and name not in keep:
                db.select_graph(name).delete()
        return {"tenant": tenant, "live": new, "previous": live_name if has_live else None,
                "changes": len(changes), "late_findings": late, "by_kind": _count(changes)}
    finally:
        r.delete(LOCK.format(tenant))


def rollback(tenant: str) -> str:
    r = connect().connection
    prev = r.get(PREVIOUS.format(tenant))
    prev = prev.decode() if isinstance(prev, bytes) else prev
    if not prev or prev not in connect().list_graphs():
        raise SystemExit("No previous graph to roll back to.")
    r.set(ALIAS.format(tenant), prev)
    return prev


def _count(changes: list[dict]) -> dict:
    out: dict[str, int] = {}
    for c in changes:
        out[c["kind"]] = out.get(c["kind"], 0) + 1
    return out


def main() -> None:
    tenant = os.getenv("LINEAGE_TENANT", "python")
    if "rollback" in sys.argv[1:]:
        print("live:", rollback(tenant))
        return
    if os.getenv("FETCH"):
        fetch()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    print(swap(tenant, f"{tenant}__{stamp}"))


if __name__ == "__main__":
    main()
