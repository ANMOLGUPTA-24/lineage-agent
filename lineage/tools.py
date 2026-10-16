"""The agent's fixed tool catalogue.

Each tool is a hand-written, parameterized Cypher query (plus FalkorDB algorithms where they
earn their place). The model only picks a tool and fills typed arguments; it never writes Cypher.
Every tool returns the same envelope:

  outcome   "answered" | "memory" | "not_in_graph" | "unknown_entity"
  rows      facts the answer may use
  paths     the graph paths that produced those facts (the explanation)
  cypher    the queries that ran, for the trace panel
"""
import re
import time
from datetime import datetime, timezone
from functools import lru_cache

from falkordb import Node, Path
from redis.exceptions import ResponseError

from lineage.db import graph  # noqa: F401  (tests reuse it)

MAX_DEPTH = 4
# Citation chains drift off-topic after 2-3 hops (typing -> packaging metadata). Ancestors farther
# than this cosine distance from the starting PEP are dropped. Tuned by hand on PEPs 572/634/695.
MAX_DRIFT = 0.27
LINEAGE_RELS = "REQUIRES|REFERENCES|SUPERSEDES"
CONNECT_RELS = ["AUTHORED", "DELEGATE_FOR", "REFERENCES", "REQUIRES", "SUPERSEDES",
                "OPENED", "IMPLEMENTS", "DISCUSSES", "RESOLVED_BY", "MADE_BY", "IN_TOPIC"]


# ---------------------------------------------------------------- helpers

def node_view(n: Node) -> dict:
    p = n.properties
    label = n.labels[0] if n.labels else "?"
    if label == "PEP":
        return {"id": f"PEP {p['number']}", "label": "PEP", "title": p.get("title"),
                "status": p.get("status"), "url": p.get("url")}
    if label in ("PR", "Issue"):
        return {"id": f"{label} #{p['number']}", "label": label, "title": p.get("title"), "url": p.get("url")}
    if label == "Decision":
        return {"id": f"Decision on PEP {p['pep']}", "label": "Decision", "outcome": p.get("outcome"),
                "url": p.get("verdict_url") or p.get("url"), "at": p.get("at")}
    if label == "Finding":
        return {"id": f"Finding {p.get('at')}", "label": "Finding", "title": p.get("text")}
    return {"id": p.get("name") or p.get("version") or str(n.id), "label": label}


def path_view(path: Path) -> list[dict]:
    """[node, edge, node, edge, node, ...]; each edge records its real direction."""
    nodes, edges = path.nodes(), path.edges()
    out = [node_view(nodes[0])]
    for i, e in enumerate(edges):
        a, b = nodes[i], nodes[i + 1]
        out.append({"edge": e.relation, "forward": e.src_node == a.id,
                    **({"inferred": True} if e.properties.get("inferred") else {})})
        out.append(node_view(b))
    return out


class Run:
    """Collects the Cypher that ran, so the UI can show it next to the answer."""

    def __init__(self, tenant: str):
        self.g = graph(tenant)
        self.cypher: list[str] = []

    def q(self, cypher: str, params: dict | None = None):
        self.cypher.append(" ".join(cypher.split()))
        return self.g.ro_query(cypher, params or {}).result_set

    def w(self, cypher: str, params: dict | None = None):
        self.cypher.append(" ".join(cypher.split()))
        return self.g.query(cypher, params or {}).result_set


def envelope(tool: str, args: dict, run: Run, outcome: str, rows=None, paths=None, note: str = "") -> dict:
    return {"tool": tool, "args": args, "outcome": outcome, "rows": rows or [], "paths": paths or [],
            "note": note, "cypher": run.cypher}


def unknown(tool: str, args: dict, run: Run, what: str) -> dict:
    return envelope(tool, args, run, "unknown_entity", note=f"No PEP, person or topic in the graph matches {what!r}.")


def pep_number(run: Run, ref: str | int) -> int | None:
    """Resolve to an existing PEP number, or None. Never guesses."""
    m = re.fullmatch(r"\s*(?:PEP[\s-]?)?(\d{1,4})\s*", str(ref), re.I)
    if m:
        n = int(m.group(1))
        return n if run.q("MATCH (p:PEP {number: $n}) RETURN p.number", {"n": n}) else None
    hits = resolve_rows(run, str(ref), kinds=("PEP",))
    return hits[0]["number"] if hits and hits[0]["confident"] else None


@lru_cache(maxsize=1)
def _embedder():
    from fastembed import TextEmbedding
    return TextEmbedding("BAAI/bge-small-en-v1.5")


def _fulltext_query(text: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9]+", text) if len(w) > 1]
    return " ".join(words)


def resolve_rows(run: Run, text: str, kinds=("PEP", "Person", "Topic"), k: int = 5) -> list[dict]:
    out: list[dict] = []
    m = re.fullmatch(r"\s*(?:PEP[\s-]?)?(\d{1,4})\s*", text, re.I)
    if m and "PEP" in kinds:
        for n, t, s in run.q("MATCH (p:PEP {number: $n}) RETURN p.number, p.title, p.status", {"n": int(m.group(1))}):
            out.append({"kind": "PEP", "number": n, "title": t, "status": s, "via": "number", "confident": True})
        return out
    ft = _fulltext_query(text)
    if not ft:
        return out
    if "Person" in kinds:
        for name, score in run.q("""CALL db.idx.fulltext.queryNodes('Person', $q) YIELD node, score
                                     RETURN node.name, score ORDER BY score DESC LIMIT 3""", {"q": ft}):
            out.append({"kind": "Person", "name": name, "via": "name", "confident": True})
    if "Topic" in kinds:
        for name, in run.q("MATCH (t:Topic) WHERE toLower($q) CONTAINS t.name RETURN t.name", {"q": text}):
            out.append({"kind": "Topic", "name": name, "via": "topic", "confident": True})
    if "PEP" in kinds:
        seen = set()
        hits = run.q("""CALL db.idx.fulltext.queryNodes('PEP', $q) YIELD node, score
                        RETURN node.number, node.title, node.status, score
                        ORDER BY score DESC LIMIT $k""", {"q": ft, "k": k})
        for i, (n, t, s, score) in enumerate(hits):
            seen.add(n)
            # Confident only when the top hit clearly beats the runner-up; otherwise the agent
            # must show the candidates instead of picking one.
            clear = i == 0 and (len(hits) == 1 or score >= 1.5 * hits[1][3])
            out.append({"kind": "PEP", "number": n, "title": t, "status": s, "via": "full-text", "confident": clear})
        vec = list(_embedder().query_embed([text]))[0].tolist()
        # cosine *distance*: lower is closer. 0.30 was tuned on a few dozen hand-checked queries.
        for n, t, s, d in run.q("""CALL db.idx.vector.queryNodes('PEP', 'embedding', $k, vecf32($v)) YIELD node, score
                                   RETURN node.number, node.title, node.status, score ORDER BY score""", {"k": k, "v": vec}):
            if n not in seen and d < 0.30:
                seen.add(n)
                out.append({"kind": "PEP", "number": n, "title": t, "status": s, "via": "semantic",
                            "confident": not out and d < 0.22})
    return out


# ---------------------------------------------------------------- tools

def resolve(text: str, tenant: str = "python") -> dict:
    """Find PEPs, people or topics matching free text. Call this before any other tool when the
    user names something without a PEP number."""
    run, args = Run(tenant), {"text": text}
    rows = resolve_rows(run, text)
    return envelope("resolve", args, run, "answered" if rows else "unknown_entity", rows=rows,
                    note="" if rows else f"Nothing in the graph matches {text!r}.")


def why(pep: str, depth: int = 2, tenant: str = "python") -> dict:
    """Decision lineage of a PEP: who proposed and decided it, where the ruling is, which earlier
    PEPs it builds on (up to `depth` hops), and the CPython PRs that implemented it.
    Ancestors: every PEP reachable within `depth` hops (skipping Process PEPs such as PEP 1 and
    PEP 8, which everything cites), each via its cheapest path, kept only if it is about the same
    thing (embedding distance < MAX_DRIFT), closest first."""
    run, args = Run(tenant), {"pep": pep, "depth": depth}
    depth = max(1, min(int(depth), MAX_DEPTH))
    n = pep_number(run, pep)
    if n is None:
        return unknown("why", args, run, pep)

    head = run.q("""MATCH (p:PEP {number: $n})
                    OPTIONAL MATCH (a:Person)-[r:AUTHORED]->(p)
                    WITH p, collect({name: a.name, role: r.role}) AS people
                    OPTIONAL MATCH (p)-[:RESOLVED_BY]->(d:Decision)-[m:MADE_BY]->(x)
                    RETURN p.title, p.status, p.created, p.python_version, p.url, people,
                           d.outcome, coalesce(d.verdict_url, d.url), d.at, d.verdict,
                           collect({by: x.name, inferred: m.inferred})""", {"n": n})[0]
    title, status, created, version, url, people, outcome, ruling, at, verdict, deciders = head
    deciders = [d for d in deciders if d["by"]]
    rows = [{"pep": n, "title": title, "status": status, "created": created, "python_version": version,
             "url": url, "people": [p for p in people if p["name"]],
             "decision": {"outcome": outcome, "ruling_url": ruling, "at": at,
                          "verdict_excerpt": (verdict or "")[:400], "decided_by": deciders} if outcome else None}]

    paths = [path_view(r[0]) for r in run.q(f"""
        MATCH path = (p:PEP {{number: $n}})-[:{LINEAGE_RELS}*1..{depth}]->(q:PEP)
        WHERE q.number <> $n AND all(x IN tail(nodes(path)) WHERE x.type <> 'Process')
        WITH p, path, q, reduce(w = 0.0, e IN relationships(path) | w + e.weight) AS cost
        ORDER BY cost
        WITH p, q, collect(path)[0] AS best
        WITH best, vec.cosineDistance(p.embedding, q.embedding) AS drift
        WHERE drift < $max_drift
        RETURN best ORDER BY drift LIMIT 12""", {"n": n, "max_drift": MAX_DRIFT})]

    paths += [path_view(r[0]) for r in run.q("""
        MATCH path = (p:PEP {number: $n})-[:RESOLVED_BY]->(:Decision)-[:MADE_BY]->()
        RETURN path""", {"n": n})]

    prs = run.q("""MATCH path = (x)-[:IMPLEMENTS|DISCUSSES]->(p:PEP {number: $n})
                   RETURN path, x.comments ORDER BY coalesce(x.merged, false) DESC, x.comments DESC LIMIT 5""", {"n": n})
    paths += [path_view(r[0]) for r in prs]

    succ = run.q("""MATCH path = (p:PEP {number: $n})<-[:SUPERSEDES*1..5]-(s:PEP) RETURN path""", {"n": n})
    paths += [path_view(r[0]) for r in succ]
    if succ:
        rows[0]["superseded_by"] = [path_view(r[0])[-1]["id"] for r in succ]

    return envelope("why", args, run, "answered", rows=rows, paths=paths,
                    note="" if len(paths) > 1 else "The graph records no lineage for this PEP at this depth.")


def is_current(pep: str, tenant: str = "python") -> dict:
    """Whether a PEP still describes how Python works: its status and the full SUPERSEDES chain
    to whatever replaced it."""
    run, args = Run(tenant), {"pep": pep}
    n = pep_number(run, pep)
    if n is None:
        return unknown("is_current", args, run, pep)
    status, title = run.q("MATCH (p:PEP {number: $n}) RETURN p.status, p.title", {"n": n})[0]
    chain = run.q("""MATCH path = (p:PEP {number: $n})<-[:SUPERSEDES*1..6]-(latest:PEP)
                     WHERE NOT ()-[:SUPERSEDES]->(latest)
                     RETURN path, latest.number, latest.status ORDER BY length(path) DESC""", {"n": n})
    replaced = run.q("""MATCH path = (p:PEP {number: $n})-[:SUPERSEDES]->(old:PEP) RETURN path""", {"n": n})
    current = status in ("Final", "Active", "Accepted", "Provisional") and not chain
    rows = [{"pep": n, "title": title, "status": status, "current": current,
             "latest": [{"pep": r[1], "status": r[2]} for r in chain],
             "replaces": [path_view(r[0])[-1]["id"] for r in replaced]}]
    note = ""
    if status in ("Rejected", "Withdrawn", "Deferred", "Draft") and not chain:
        note = f"PEP {n} is {status}; it never became part of Python."
    return envelope("is_current", args, run, "answered", rows=rows,
                    paths=[path_view(r[0]) for r in chain + replaced], note=note)


def _subject_peps(run: Run, subject: str) -> tuple[str, list[int]] | None:
    t = run.q("MATCH (t:Topic) WHERE t.name = toLower($s) RETURN t.name", {"s": subject.strip()})
    if t:
        nums = [r[0] for r in run.q("MATCH (p:PEP)-[:IN_TOPIC]->(:Topic {name: $t}) RETURN p.number", {"t": t[0][0]})]
        return f"topic {t[0][0]}", nums
    n = pep_number(run, subject)
    if n is None:
        return None
    # A PEP's neighbourhood: itself plus the PEPs one lineage hop away in either direction.
    nums = [r[0] for r in run.q(f"""MATCH (p:PEP {{number: $n}}) OPTIONAL MATCH (p)-[:{LINEAGE_RELS}]-(q:PEP)
                                    WITH p, collect(DISTINCT q.number) AS near RETURN [p.number] + near""", {"n": n})[0]]
    return f"PEP {n}", nums[0]


def who_knows(subject: str, limit: int = 5, tenant: str = "python") -> dict:
    """People who know an area (a topic such as 'typing', or a PEP and its neighbours), ranked by
    what they authored, decided and implemented, with a betweenness 'bridge' score."""
    run, args = Run(tenant), {"subject": subject, "limit": limit}
    found = _subject_peps(run, subject)
    if not found:
        return unknown("who_knows", args, run, subject)
    label, nums = found
    rows = run.q("""
        MATCH (p:PEP) WHERE p.number IN $nums
        MATCH (a:Person)-[r:AUTHORED|DELEGATE_FOR]->(p)
        WITH a, type(r) AS role, 3 AS pts, p AS x
        RETURN a, role, pts, x
        UNION ALL
        MATCH (p:PEP) WHERE p.number IN $nums
        MATCH (a:Person)-[:OPENED]->(x)-[:IMPLEMENTS|DISCUSSES]->(p)
        RETURN a, 'OPENED' AS role, CASE WHEN x.merged THEN 2 ELSE 1 END AS pts, x
        """, {"nums": nums})
    agg: dict[str, dict] = {}
    for a, role, pts, x in rows:
        e = agg.setdefault(a.properties["name"], {"name": a.properties["name"], "score": 0, "roles": set(), "items": set(),
                                                   "bridge": round(a.properties.get("bridge") or 0.0, 1)})
        e["score"] += pts
        e["roles"].add(role)
        e["items"].add(x.id)
    rows = sorted(agg.values(), key=lambda e: (-e["score"], -e["bridge"]))[:max(1, min(limit, 20))]
    if not rows:
        return envelope("who_knows", args, run, "not_in_graph",
                        note=f"No authors, deciders or implementers are recorded for {label}.")
    people = [{**e, "roles": sorted(e["roles"]), "items": len(e["items"])} for e in rows]
    paths = []
    for p in people[:3]:
        paths += [path_view(r[0]) for r in run.q("""
            MATCH path = (a:Person {name: $name})-[:AUTHORED|DELEGATE_FOR]->(x:PEP) WHERE x.number IN $nums
            RETURN path LIMIT 3""", {"name": p["name"], "nums": nums})]
    return envelope("who_knows", args, run, "answered", rows=[{"subject": label, "people": people}], paths=paths)


def bus_factor(subject: str, tenant: str = "python") -> dict:
    """Knowledge concentration in an area: for each PEP, how many distinct people (authors,
    deciders, implementers) are attached, and what is orphaned if the top person left."""
    run, args = Run(tenant), {"subject": subject}
    found = _subject_peps(run, subject)
    if not found:
        return unknown("bus_factor", args, run, subject)
    label, nums = found
    per_pep = run.q("""
        MATCH (p:PEP) WHERE p.number IN $nums
        OPTIONAL MATCH (a:Person)-[:AUTHORED|DELEGATE_FOR]->(p)
        OPTIONAL MATCH (b:Person)-[:OPENED]->(:PR)-[:IMPLEMENTS]->(p)
        WITH p, collect(DISTINCT a.name) + collect(DISTINCT b.name) AS ppl
        UNWIND (CASE WHEN size(ppl) = 0 THEN [null] ELSE ppl END) AS person
        WITH p, collect(DISTINCT person) AS people
        RETURN p.number, p.title, people""", {"nums": nums})
    counts: dict[str, int] = {}
    for _, _, people in per_pep:
        for name in people:
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return envelope("bus_factor", args, run, "not_in_graph", note=f"No people recorded for {label}.")
    top = max(counts, key=counts.get)
    orphaned = [{"pep": n, "title": t} for n, t, people in per_pep if people == [top]]
    single = [{"pep": n, "title": t, "only": people[0]} for n, t, people in per_pep if len(people) == 1]
    rows = [{"subject": label, "peps": len(per_pep), "people": len(counts),
             "most_involved": {"name": top, "peps": counts[top], "share": round(counts[top] / len(per_pep), 2)},
             "orphaned_if_they_leave": orphaned, "single_person_peps": len(single)}]
    paths = [path_view(r[0]) for r in run.q("""MATCH path = (a:Person {name: $top})-[:AUTHORED|DELEGATE_FOR]->(p:PEP)
                                               WHERE p.number IN $nums RETURN path LIMIT 6""",
                                            {"top": top, "nums": [o["pep"] for o in orphaned] or nums})]
    return envelope("bus_factor", args, run, "answered", rows=rows, paths=paths)


def _endpoint(run: Run, ref: str):
    """Match clause + params for a PEP or a Person."""
    n = None
    if re.fullmatch(r"\s*(?:PEP[\s-]?)?\d{1,4}\s*", ref, re.I):
        n = pep_number(run, ref)
    if n is None:
        people = run.q("MATCH (a:Person) WHERE toLower(a.name) = toLower($s) RETURN a.name", {"s": ref.strip()})
        if people:
            return "Person", {"name": people[0][0]}
        n = pep_number(run, ref)
    return ("PEP", {"number": n}) if n is not None else (None, None)


def connection(a: str, b: str, tenant: str = "python") -> dict:
    """Shortest weighted path between two PEPs and/or people across every relationship in the
    graph: authorship, decisions, PEP links, implementing PRs."""
    run, args = Run(tenant), {"a": a, "b": b}
    la, pa = _endpoint(run, a)
    lb, pb = _endpoint(run, b)
    if not la:
        return unknown("connection", args, run, a)
    if not lb:
        return unknown("connection", args, run, b)
    ka, kb = next(iter(pa)), next(iter(pb))
    rows = run.q(f"""MATCH (x:{la} {{{ka}: $va}}), (y:{lb} {{{kb}: $vb}})
                     CALL algo.SPpaths({{sourceNode: x, targetNode: y, relTypes: $rels, weightProp: 'weight',
                                         maxLen: 6, relDirection: 'both', pathCount: 3}})
                     YIELD path, pathWeight RETURN path, pathWeight ORDER BY pathWeight""",
                 {"va": pa[ka], "vb": pb[kb], "rels": CONNECT_RELS})
    if not rows:
        return envelope("connection", args, run, "not_in_graph",
                        note=f"No path of 6 hops or fewer links {a} and {b} in the graph. "
                             "That means the graph doesn't record one, not that none exists.")
    return envelope("connection", args, run, "answered",
                    rows=[{"hops": len(r[0].edges()), "cost": round(r[1], 2)} for r in rows],
                    paths=[path_view(r[0]) for r in rows])


def recall(pep: str, tenant: str = "python") -> dict:
    """Findings saved in earlier sessions about a PEP, newest first."""
    run, args = Run(tenant), {"pep": pep}
    n = pep_number(run, pep)
    if n is None:
        return unknown("recall", args, run, pep)
    rows = run.q("""MATCH path = (f:Finding)-[:ABOUT]->(p:PEP {number: $n})
                    RETURN path, f.text, f.at, f.session ORDER BY f.at DESC LIMIT 5""", {"n": n})
    if not rows:
        return envelope("recall", args, run, "not_in_graph", note=f"No saved findings about PEP {n}.")
    return envelope("recall", args, run, "memory",
                    rows=[{"text": r[1], "at": r[2], "session": r[3]} for r in rows],
                    paths=[path_view(r[0]) for r in rows])


def remember(pep: str, text: str, session: str, tenant: str = "python") -> dict:
    """Save a finding about a PEP so later sessions can recall it."""
    run, args = Run(tenant), {"pep": pep, "text": text}
    n = pep_number(run, pep)
    if n is None:
        return unknown("remember", args, run, pep)
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run.w("""MATCH (p:PEP {number: $n})
             CREATE (f:Finding {text: $text, at: $at, session: $session})-[:ABOUT {weight: 1.0}]->(p)""",
          {"n": n, "text": text[:500], "at": at, "session": session})
    return envelope("remember", args, run, "answered", rows=[{"saved": True, "pep": n, "at": at}])


TOOLS = {f.__name__: f for f in (resolve, why, is_current, who_knows, bus_factor, connection, recall, remember)}


def call(name: str, args: dict, tenant: str = "python") -> dict:
    t0 = time.perf_counter()
    if name not in TOOLS:
        return {"tool": name, "args": args, "outcome": "error", "note": f"Unknown tool {name}", "rows": [], "paths": []}
    try:
        res = TOOLS[name](**args, tenant=tenant)
    except TypeError as e:
        res = {"tool": name, "args": args, "outcome": "error", "note": f"Bad arguments: {e}", "rows": [], "paths": []}
    except ResponseError as e:
        if "empty key" not in str(e):
            raise
        res = {"tool": name, "args": args, "outcome": "unknown_entity", "rows": [], "paths": [],
               "note": f"Tenant {tenant!r} has no graph yet."}
    res["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return res
