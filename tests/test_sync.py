"""Sync's swap logic on tiny graphs (no full ingest): diff, carry-over, refusal, lock, rollback."""
import pytest

from lineage.db import ALIAS, connect, resolve
from lineage.ingest import sync

T = "synctest"


def tiny(name: str, peps: list[tuple[int, str]], supersedes=(), finding: str | None = None):
    g = connect().select_graph(name)
    g.query("UNWIND $rows AS r CREATE (:PEP {number: r[0], status: r[1], title: 'PEP ' + toString(r[0])})",
            {"rows": [list(p) for p in peps]})
    for a, b in supersedes:
        g.query("MATCH (a:PEP {number: $a}), (b:PEP {number: $b}) CREATE (a)-[:SUPERSEDES]->(b)", {"a": a, "b": b})
    if finding:
        g.query("MATCH (p:PEP {number: 1}) CREATE (:Finding {text: $t, at: '2026-01-01', session: 's'})-[:ABOUT]->(p)",
                {"t": finding})
    return g


@pytest.fixture(autouse=True)
def clean():
    def wipe():
        db = connect()
        for name in db.list_graphs():
            if name == T or name.startswith(T + "__"):
                db.select_graph(name).delete()
        for key in (ALIAS, sync.PREVIOUS, sync.LOCK):
            db.connection.delete(key.format(T))
    wipe()
    yield
    wipe()


def test_swap_records_changes_and_keeps_memory():
    tiny(T, [(1, "Draft"), (2, "Final")], finding="remember me")
    res = sync.swap(T, f"{T}__v2", build_fn=lambda n: tiny(n, [(1, "Rejected"), (2, "Final"), (3, "Draft")], supersedes=[(3, 2)]))
    assert resolve(T) == f"{T}__v2" and res["previous"] == T
    assert res["by_kind"] == {"status": 1, "new_pep": 1, "superseded": 1}
    g = connect().select_graph(f"{T}__v2")
    assert g.ro_query("MATCH (c:Change)-[:CHANGED]->(:PEP {number: 1}) RETURN c.detail").result_set == [["PEP 1 status Draft -> Rejected"]]
    assert g.ro_query("MATCH (f:Finding {text: 'remember me'}) RETURN count(f)").result_set == [[1]]


def test_refuses_to_swap_a_broken_build():
    tiny(T, [(n, "Final") for n in range(1, 41)])
    with pytest.raises(SystemExit, match="Refusing to swap"):
        sync.swap(T, f"{T}__bad", build_fn=lambda n: tiny(n, [(1, "Final")]))
    assert resolve(T) == T and f"{T}__bad" not in connect().list_graphs()


def test_lock_blocks_a_second_sync():
    tiny(T, [(1, "Final")])
    connect().connection.set(sync.LOCK.format(T), "someone-else")
    with pytest.raises(SystemExit, match="Another sync"):
        sync.swap(T, f"{T}__v2", build_fn=lambda n: tiny(n, [(1, "Final")]))
    assert resolve(T) == T


def test_rollback_points_back_at_the_previous_graph(monkeypatch):
    tiny(T, [(1, "Final")])
    sync.swap(T, f"{T}__v2", build_fn=lambda n: tiny(n, [(1, "Final"), (2, "Draft")]))
    assert sync.rollback(T) == T and resolve(T) == T


def test_session_memory_survives_the_swap():
    g = tiny(T, [(1, "Final"), (2, "Draft")])
    g.query("""CREATE (s:Session {id: 'abc', started: '2026-01-01'})-[:ASKED]->(q:Query {text: 'why 1', at: '2026-01-01'})
               WITH q MATCH (p:PEP {number: 1}) CREATE (q)-[:ACCESSED]->(p)""")
    sync.swap(T, f"{T}__v2", build_fn=lambda n: tiny(n, [(1, "Final"), (2, "Draft")]))
    new = connect().select_graph(f"{T}__v2")
    assert new.ro_query("""MATCH (:Session {id: 'abc'})-[:ASKED]->(:Query {text: 'why 1'})-[:ACCESSED]->(p:PEP)
                           RETURN p.number""").result_set == [[1]]
