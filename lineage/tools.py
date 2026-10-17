"""The agent's fixed tool catalogue.

Each tool is a hand-written, parameterized Cypher query (plus FalkorDB algorithms where they
earn their place). The model only picks a tool and fills typed arguments; it never writes Cypher.
Every tool returns the same envelope:

  outcome   "answered" | "memory" | "not_in_graph" | "unknown_entity"
  rows      facts the answer may use
  paths     the graph paths that produced those facts (the explanation)
  cypher    the queries that ran, for the trace panel
"""
import inspect
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
NOT_ADOPTED = {"Rejected", "Withdrawn", "Deferred"}
QUOTE_CHARS = 320
CONNECT_RELS = ["AUTHORED", "DELEGATE_FOR", "REFERENCES", "REQUIRES", "SUPERSEDES", "REJECTS_APPROACH_OF",
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
    if label == "Session":
        return {"id": f"Session {p.get('id')}", "label": "Session"}
    if label == "Query":
        return {"id": f"Asked: {(p.get('text') or '')[:48]}", "label": "Query", "title": p.get("text")}
    if label == "Section":
        return {"id": f"{p.get('title')} (PEP {p.get('pep')})", "label": "Section", "title": p.get("title"), "url": p.get("url")}
    if label == "Thread":
        return {"id": f"Thread: {p.get('title', '')[:60]}", "label": "Thread", "title": p.get("title"), "url": p.get("url")}
    if label == "Change":
        return {"id": f"Change {p.get('at', '')[:10]}: {p.get('kind')}", "label": "Change", "title": p.get("detail")}
    if label == "Alternative":
        return {"id": f"Rejected idea: {p.get('title')} (PEP {p.get('pep')})", "label": "Alternative",
                "title": p.get("title"), "url": p.get("url")}
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
        self.params: list[dict] = []
        self.notes: list[str] = []

    def say(self, text: str) -> None:
        """A step a reader can follow, written from what actually ran (counts, scores), never ahead of it."""
        self.notes.append(text)

    def log(self, cypher: str, params: dict | None) -> dict:
        params = params or {}
        self.cypher.append(" ".join(cypher.split()))
        # Embedding vectors are 384 floats; the trace shows their size, not the numbers.
        self.params.append({k: f"<vector[{len(v)}]>" if k == "v" else v for k, v in params.items()})
        return params

    def q(self, cypher: str, params: dict | None = None):
        return self.g.ro_query(cypher, self.log(cypher, params)).result_set

    def w(self, cypher: str, params: dict | None = None):
        return self.g.query(cypher, self.log(cypher, params)).result_set


def first_sentences(text: str, limit: int = QUOTE_CHARS) -> str:
    """Opening text of a source, cut at a sentence end. Stub paragraphs ("Rejected.") are skipped
    in favour of the first one that says something; the guard checks quotes against these rows."""
    paras = (text or "").split("\n\n")
    text = next((p for p in paras if len(p) >= 60), paras[0])
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = cut.rfind(". ")
    return cut[:end + 1] if end > 80 else cut.rstrip() + "…"


def envelope(tool: str, args: dict, run: Run, outcome: str, rows=None, paths=None, note: str = "") -> dict:
    return {"tool": tool, "args": args, "outcome": outcome, "rows": rows or [], "paths": paths or [],
            "note": note, "cypher": run.cypher, "params": run.params, "explain": run.notes}


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

    # The PEP's own words: why it exists (Rationale, else Motivation) and, if any, its ruling notice.
    for kind, title, text, surl in run.q("""MATCH (p:PEP {number: $n})-[:HAS_SECTION]->(s:Section)
                                            WHERE s.kind IN ['rationale', 'motivation', 'notice']
                                            RETURN s.kind, s.title, s.text, s.url""", {"n": n}):
        key = "ruling_notice" if kind == "notice" else "rationale"
        if key not in rows[0] or (key == "rationale" and kind == "rationale"):
            rows[0][key] = {"section": title, "quote": first_sentences(text), "url": surl}

    run.say(f"Variable-length traversal REQUIRES|REFERENCES|SUPERSEDES*1..{depth} from PEP {n}, cheapest path per "
            f"ancestor, kept {len(paths)} within embedding drift {MAX_DRIFT}")
    # A cited PEP that never shipped is an earlier proposal, not something this PEP builds on.
    ancestors = [p[-1] for p in paths]
    rows[0]["builds_on"] = [a["id"] for a in ancestors if a.get("status") not in NOT_ADOPTED]
    rows[0]["earlier_proposals"] = [{"pep": a["id"], "status": a["status"]} for a in ancestors
                                    if a.get("status") in NOT_ADOPTED]

    # Approaches this PEP explicitly set aside, quoted from its own rejected-ideas section.
    rejected = run.q("""MATCH path = (p:PEP {number: $n})<-[:REJECTED_IN]-(a:Alternative)-[c:CITES]->(q:PEP)
                        WHERE (p)-[:REJECTS_APPROACH_OF]->(q)
                        RETURN path, q.number, q.status, a.title, CASE WHEN coalesce(c.quote, '') = '' THEN a.text ELSE c.quote END, a.url""", {"n": n})
    rows[0]["rejected_approaches"] = [{"pep": qn, "status": qs, "idea": t, "quote": first_sentences(txt), "url": u}
                                      for _, qn, qs, t, txt, u in rejected]
    paths += [path_view(r[0]) for r in rejected]

    # The other direction: later PEPs that set this PEP's approach aside (the idea came back).
    later = run.q("""MATCH path = (p:PEP {number: $n})<-[x:REJECTS_APPROACH_OF]-(l:PEP)
                     RETURN path, l.number, l.created, x.via ORDER BY l.created""", {"n": n})
    rows[0]["set_aside_later_by"] = [{"pep": ln, "created": lc, "url": via[0]} for _, ln, lc, via in later]
    paths += [path_view(r[0]) for r in later]

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
    run.say(f"Followed SUPERSEDES edges from PEP {n} (status {status}): "
            + (f"{len(chain)} chain(s) to the latest PEP" if chain else "nothing supersedes it"))
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


SCOPE_PEPS = {"Topic": "(q:PEP)-[:IN_TOPIC]->(:Topic {name: $key})",
              "Community": "(q:PEP)-[:IN_COMMUNITY]->(:Community {id: $key})"}


def _scope(run: Run, subject: str) -> dict | None:
    """The area a subject belongs to: a Topic by name, or for a PEP its Topic, else its Community.
    Only scopes with a precomputed expertise projection (ingest/expertise.py) qualify."""
    rows = run.q("""MATCH (s:Topic) WHERE s.name = toLower($s) AND s.slug IS NOT NULL
                    RETURN 'Topic' AS kind, s.name AS key, 'topic ' + s.name AS name, s""",
                 {"s": subject.strip()})
    pep = None
    if not rows:
        pep = pep_number(run, subject)
        if pep is None:
            return None
        rows = run.q("""MATCH (p:PEP {number: $n})
                        OPTIONAL MATCH (p)-[:IN_TOPIC]->(t:Topic) WHERE t.slug IS NOT NULL
                        OPTIONAL MATCH (p)-[:IN_COMMUNITY]->(c:Community) WHERE c.slug IS NOT NULL
                        WITH coalesce(t, c) AS s WHERE s IS NOT NULL
                        RETURN labels(s)[0], CASE WHEN s:Topic THEN s.name ELSE s.id END,
                               CASE WHEN s:Topic THEN 'topic ' + s.name ELSE 'the cluster around PEP ' + toString(s.hub) END, s
                        LIMIT 1""", {"n": pep})
        if not rows:
            return {"pep": pep}
    kind, key, name, node = rows[0]
    return {"kind": kind, "key": key, "name": name, "slug": node.properties["slug"], "pep": pep, **{
        k: node.properties.get(k) for k in ("people", "top", "largest", "largest_without_top", "groups_without_top")}}


def who_knows(subject: str, limit: int = 5, tenant: str = "python") -> dict:
    """People who know an area (a topic such as 'typing', or a PEP's area). Ranked by PageRank over
    the area's influence graph (who the area's PEPs and PRs build on: citations, implementations,
    decisions), with the most active people listed separately. For a PEP, its own authors and
    decider come first."""
    run, args = Run(tenant), {"subject": subject, "limit": limit}
    sc = _scope(run, subject)
    if sc is None:
        return unknown("who_knows", args, run, subject)
    if "slug" not in sc:
        return _who_knows_local(run, args, subject, limit)
    limit = max(1, min(int(limit), 20))
    experts = run.q("""MATCH (p:Person)-[e:EXPERT_IN {scope: $slug}]->()
                       RETURN p.name, e.rank, e.share, e.items ORDER BY e.rank""", {"slug": sc["slug"]})
    people = [{"name": n, "rank": r, "influence_share": s, "items": i} for n, r, s, i in experts]
    direct = []
    if sc["pep"] is not None:
        direct = [{"name": n, "role": role.lower()} for n, role in run.q(
            "MATCH (a:Person)-[r:AUTHORED|DELEGATE_FOR]->(:PEP {number: $n}) RETURN a.name, type(r)", {"n": sc["pep"]})]
    paths = []
    for p in people[:3]:
        paths += [path_view(r[0]) for r in run.q(f"""
            MATCH path = (a:Person)-[:AUTHORED|OPENED]->(x)-[:REQUIRES|REFERENCES|SUPERSEDES|IMPLEMENTS|DISCUSSES]->
                         (q:PEP)<-[:AUTHORED|DELEGATE_FOR]-(b:Person {{name: $name}})
            WHERE a <> b AND {SCOPE_PEPS[sc['kind']]}
            RETURN path LIMIT 2""", {"name": p["name"], "key": sc["key"]})]
    run.say(f"Read EXPERT_IN for {sc['name']}: PageRank over the INFLUENCE_{sc['slug']} projection (Person -> Person "
            "when one's PEP or PR builds on, or was decided by, the other's work), computed at ingest")
    run.say(f"Evidence: {len(paths)} citation path(s) into the top-ranked people's PEPs")
    row = {"subject": sc["name"], "people_in_area": sc["people"], "direct": direct,
           "most_built_on": people[:limit],
           "most_active": sorted(people, key=lambda p: -p["items"])[:3],
           "method": f"PageRank over INFLUENCE_{sc['slug']}"}
    return envelope("who_knows", args, run, "answered", rows=[row], paths=paths)


def _who_knows_local(run: Run, args: dict, subject: str, limit: int) -> dict:
    """Fallback for a PEP outside every expertise scope: count who authored, decided and
    implemented the PEP and its direct neighbours."""
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
    sc = _scope(run, subject)
    if sc is None:
        return unknown("bus_factor", args, run, subject)
    if "slug" in sc:
        label = sc["name"]
        nums = [r[0] for r in run.q(f"MATCH {SCOPE_PEPS[sc['kind']]} RETURN q.number", {"key": sc["key"]})]
    elif found := _subject_peps(run, subject):
        label, nums = found
    else:
        return unknown("bus_factor", args, run, subject)
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
    # The person whose departure matters most: the area's top PageRank person when the area has
    # a projection, otherwise the person attached to the most PEPs.
    top = sc.get("top") if sc.get("top") in counts else max(counts, key=counts.get)
    orphaned = [{"pep": n, "title": t} for n, t, people in per_pep if people == [top]]
    single = [{"pep": n, "title": t, "only": people[0]} for n, t, people in per_pep if len(people) == 1]
    rows = [{"subject": label, "peps": len(per_pep), "people": len(counts),
             "most_involved": {"name": top, "peps": counts[top], "share": round(counts[top] / len(per_pep), 2)},
             "orphaned_if_they_leave": orphaned, "single_person_peps": len(single)}]
    run.say(f"Counted distinct authors, deciders and implementers on each of {len(per_pep)} PEPs; "
            f"{len(single)} depend on one person")
    if "slug" in sc and sc["largest"]:
        run.say(f"algo.WCC over INFLUENCE_{sc['slug']} with and without {top} (computed at ingest): "
                f"largest connected group {sc['largest']} -> {sc['largest_without_top']}")
        # WCC at ingest: the largest connected group of the area's people, with and without `top`.
        rows[0]["connected"] = {"largest_group": sc["largest"], "largest_without_them": sc["largest_without_top"],
                                "cut_off_if_they_leave": max(0, sc["largest"] - 1 - sc["largest_without_top"]),
                                "groups_without_them": sc["groups_without_top"],
                                "method": f"algo.WCC over INFLUENCE_{sc['slug']}"}
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
    run.say(f"algo.SPpaths: weighted shortest paths (up to 6 hops, both directions) over {len(CONNECT_RELS)} "
            f"relationship types; {len(rows)} path(s), cheapest cost {rows[0][1]:.2f} in {len(rows[0][0].edges())} hop(s). "
            "Person and topic edges cost 1 + ln(1 + degree), so hubs don't win by default")
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


def changes(since: str = "", pep: str = "", tenant: str = "python") -> dict:
    """What changed in the graph at recent syncs (`make sync`): new PEPs, status changes, new
    supersessions, new rulings, newly linked PRs and issues. Optional `since` (ISO date) and `pep`."""
    run, args = Run(tenant), {"since": since, "pep": pep}
    last = run.q("MATCH (s:Sync) RETURN s.at, s.changes, s.previous ORDER BY s.at DESC LIMIT 1")
    if not last:
        return envelope("changes", args, run, "not_in_graph",
                        note="This graph has not been synced since it was built, so no changes are recorded.")
    n = None
    if pep:
        n = pep_number(run, pep)
        if n is None:
            return unknown("changes", args, run, pep)
    found = run.q("""MATCH path = (c:Change)-[:CHANGED]->(p:PEP)
                     WHERE c.at >= $since AND ($n IS NULL OR p.number = $n)
                     RETURN path, c.at, c.kind, c.detail ORDER BY c.at DESC, p.number LIMIT 50""",
                  {"since": since, "n": n})
    at, count, prev = last[0]
    rows = [{"last_sync": at, "changes_at_last_sync": count, "previous_graph": prev,
             "changes": [{"at": a, "kind": k, "detail": d} for _, a, k, d in found]}]
    return envelope("changes", args, run, "answered", rows=rows, paths=[path_view(r[0]) for r in found[:12]],
                    note="" if found else "No recorded changes match.")


# ---------------------------------------------------------------- already decided?

# Vector distance alone can't tell "same idea" from "same vocabulary" (multi-line lambdas land
# 0.21 from conditional expressions, switch statements 0.30 from PEP 3103). Vector + full-text
# only gather candidates; a cross-encoder scores (idea, candidate) pairs. Bands set on 12
# hand-checked ideas: true matches scored 6.0-7.9, loosely related history ~2, ideas with no
# PEP -0.4 or lower. MiniLM-L-6 on 300-char snippets matched L-12's top hits at a third of the
# latency (~0.45 s for 60 candidates on CPU).
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
SAME_IDEA, RELATED_IDEA = 5.0, 1.0
SNIPPET = 300
STOP = {"the", "and", "for", "add", "use", "with", "like", "allow", "make", "new", "python", "should", "can", "why", "not"}
VERDICT = {"Final": "adopted", "Accepted": "adopted", "Active": "adopted", "Provisional": "adopted provisionally",
           "Rejected": "rejected", "Withdrawn": "withdrawn by its authors", "Deferred": "deferred, never decided",
           "Draft": "still a draft", "Superseded": "superseded"}


# Question framing around the idea itself. The cross-encoder scores "add a do-while loop" 7.8
# but "Has anyone proposed a do-while loop?" only ~4, so the framing is removed first.
FRAMING = re.compile(r"""^\s*(?:(?:can\s+you\s+|please\s+)?(?:draft|write)\s+(?:a\s+)?(?:reply|response|answer)\s+
                         (?:to\s+)?(?:someone|a\s+user|a\s+post|a\s+thread|the\s+idea)?\s*(?:who\s+is\s+|who's\s+)?
                         (?:proposing|suggesting|asking\s+for|that\s+proposes|about|for|on)?\s*:?
                       | (?:reply|respond)\s+to\s*:?
                       | (?:has|have|did)\s+(?:anyone|someone|python|the\s+core\s+devs?)\s+(?:ever\s+|already\s+)?
                         (?:proposed|suggested|considered|discussed|decided(?:\s+on|\s+about|\s+whether)?|rejected)
                       | (?:was|is|were)\s+(?:there\s+(?:ever\s+)?a\s+pep\s+(?:for|about|on|to)|.{0,0})
                       | why\s+(?:not|doesn't\s+python\s+have|does\s+not\s+python\s+have|no|isn't\s+there)
                       | (?:idea|proposal)\s*:
                     )\s*""", re.I | re.X)


def idea_text(text: str) -> str:
    out = FRAMING.sub("", text, count=1).strip().rstrip("?.!").strip()
    tail = re.compile(r"\s+(?:(?:ever\s+)?(?:been\s+)?(?:proposed|suggested|considered|rejected|decided)(?:\s+before)?"
                      r"|(?:a\s+)?good\s+idea|worth\s+(?:it|adding))$", re.I)
    if tail.search(out):
        out = re.sub(r"^(?:have|has|was|were|is|are)\s+", "", tail.sub("", out), flags=re.I)
    return out or text


@lru_cache(maxsize=1)
def _reranker():
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    return TextCrossEncoder(RERANK_MODEL)


def idea_candidates(run: Run, idea: str, k: int = 15) -> list[dict]:
    vec = list(_embedder().query_embed([idea]))[0].tolist()
    words = [w for w in re.findall(r"[A-Za-z]{3,}", idea) if w.lower() not in STOP]
    pep = "RETURN 'pep', node.number, node.title, node.title + '. ' + coalesce(node.abstract, '')"
    alt = "RETURN 'alt', node.url, node.title, node.title + '. ' + node.text"
    queries = [(f"CALL db.idx.vector.queryNodes('PEP', 'embedding', $k, vecf32($v)) YIELD node {pep}", True),
               (f"CALL db.idx.vector.queryNodes('Alternative', 'embedding', $k, vecf32($v)) YIELD node {alt}", True),
               (f"CALL db.idx.fulltext.queryNodes('PEP', $q) YIELD node {pep} LIMIT $k", False),
               (f"CALL db.idx.fulltext.queryNodes('Alternative', $q) YIELD node {alt} LIMIT $k", False)]
    thread = "RETURN 'thread', node.id, node.title, node.title + '. ' + coalesce(node.text, '')"
    optional = [(f"CALL db.idx.vector.queryNodes('Thread', 'embedding', $k, vecf32($v)) YIELD node {thread}", True),
                (f"CALL db.idx.fulltext.queryNodes('Thread', $q) YIELD node {thread} LIMIT $k", False)]
    found: dict = {}
    for i, (cypher, vector) in enumerate(queries + optional):
        if not vector and not words:
            continue
        try:
            got = run.q(cypher, {"k": k, "v": vec, "q": "|".join(words)})
        except ResponseError:
            if i < len(queries):
                raise
            got = []  # a graph built without discuss.python.org threads has no Thread index
        for kind, key, title, text in got:
            found[(kind, key)] = {"kind": kind, "key": key, "title": title, "text": text[:SNIPPET]}
        if got:
            src = {"pep": "PEPs", "alt": "rejected ideas", "thread": "forum threads"}[got[0][0]]
            run.say(f"{'Vector' if vector else 'Full-text'} index over {src}: {len(got)} candidates")
    cands = list(found.values())
    for c, s in zip(cands, _reranker().rerank(idea, [c["text"] for c in cands])):
        c["score"] = round(float(s), 2)
    cands.sort(key=lambda c: -c["score"])
    same = sum(c["score"] >= SAME_IDEA for c in cands)
    related = sum(RELATED_IDEA <= c["score"] < SAME_IDEA for c in cands)
    run.say(f"Local cross-encoder scored {len(cands)} distinct candidates against the idea: {same} same idea "
            f"(score >= {SAME_IDEA}), {related} related, {len(cands) - same - related} dropped"
            + (f"; best: {cands[0]['title'][:60]!r} at {cands[0]['score']}" if cands else ""))
    return cands


def pep_context(run: Run, n: int) -> tuple[dict, list]:
    """Ruling, decider, supersession and later re-rejections of one PEP, in one traversal."""
    (num, title, status, created, url, abstract, authors, rulings, decisions, lines, npaths, notice, backs, later), = run.q("""
        MATCH (p:PEP {number: $n})
        OPTIONAL MATCH (au:Person)-[:AUTHORED]->(p)
        WITH p, collect(DISTINCT au.name) AS authors
        OPTIONAL MATCH ruling = (p)-[:RESOLVED_BY]->(d:Decision)-[m:MADE_BY]->(who)
        WITH p, authors, collect(ruling) AS rulings,
             collect({outcome: d.outcome, by: who.name, inferred: m.inferred, at: d.at,
                      url: coalesce(d.verdict_url, d.url)}) AS decisions
        OPTIONAL MATCH line = (p)<-[:SUPERSEDES*1..5]-(:PEP)
        WITH p, authors, rulings, decisions, collect(line) AS lines
        OPTIONAL MATCH npath = (p)-[:HAS_SECTION]->(s:Section {kind: 'notice'})
        WITH p, authors, rulings, decisions, lines, collect(npath) AS npaths,
             collect({text: s.text, url: s.url, title: s.title})[0] AS notice
        OPTIONAL MATCH back = (later:PEP)-[x:REJECTS_APPROACH_OF]->(p)
        RETURN p.number, p.title, p.status, p.created, p.url, coalesce(p.abstract, ''), authors, rulings,
               decisions, lines, npaths, notice, collect(back),
               collect({pep: later.number, title: later.title, created: later.created, via: x.via})""", {"n": n})
    decided = [d for d in decisions if d["by"]]
    succ = sorted((path_view(l) for l in lines), key=len)
    row = {"pep": num, "title": title, "status": status, "verdict": VERDICT.get(status, status.lower()),
           "created": created, "url": url, "quote": first_sentences(abstract), "authors": authors,
           "reason": ({"section": notice["title"], "quote": first_sentences(notice["text"], 420), "url": notice["url"]}
                      if notice and notice["text"] else None),
           "decision": ({"outcome": decided[0]["outcome"], "at": decided[0]["at"], "url": decided[0]["url"],
                         "decided_by": [{"by": d["by"], "inferred": bool(d["inferred"])} for d in decided]}
                        if decided else None),
           "superseded_by": succ[-1][-1]["id"] if succ else None,
           "came_back": sorted(({"pep": x["pep"], "title": x["title"], "created": x["created"], "url": x["via"][0]}
                                for x in later if x["pep"]), key=lambda x: x["created"] or "")}
    if row["came_back"]:
        # The sentence in each later PEP that set this one aside, so the comeback is quoted, not asserted.
        quotes = dict(run.q("""MATCH (a:Alternative)-[c:CITES]->(:PEP {number: $n}) WHERE a.url IN $urls
                               RETURN a.url, c.quote""", {"n": n, "urls": [b["url"] for b in row["came_back"]]}))
        for b in row["came_back"]:
            b["quote"] = quotes.get(b["url"]) or ""
    return row, [path_view(r) for r in rulings + npaths] + succ + [path_view(b) for b in backs]


# ---------------------------------------------------------------- session memory (in the graph)
#
#   (:Session {id})-[:ASKED]->(:Query {text, at, mode})-[:ACCESSED]->(:PEP)
#
# Written after each answer, per tenant graph. session_link then asks the graph how the PEP just
# answered connects to PEPs asked about earlier in the same session. make sync carries these over.

SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")


def record_turn(session: str, question: str, mode: str, peps: list[int], tenant: str = "python") -> None:
    if not SESSION_ID.fullmatch(session or ""):
        return
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    graph(tenant).query("""MERGE (s:Session {id: $sid}) ON CREATE SET s.started = $at
                           CREATE (s)-[:ASKED {weight: 1.0}]->(q:Query {text: $text, at: $at, mode: $mode})
                           WITH q UNWIND $peps AS n MATCH (p:PEP {number: n})
                           CREATE (q)-[:ACCESSED {weight: 1.0}]->(p)""",
                        {"sid": session, "at": at, "text": question[:500], "mode": mode, "peps": peps[:5]})


def session_link(session: str, peps: list[int], tenant: str = "python") -> dict:
    """How the PEPs in this answer connect to PEPs asked about earlier in the same session:
    a weighted shortest path (algo.SPpaths, at most 3 hops) to the nearest earlier PEP."""
    run, args = Run(tenant), {"session": session, "peps": peps}
    if not SESSION_ID.fullmatch(session or "") or not peps:
        return envelope("session_link", args, run, "not_in_graph", note="No earlier questions in this session.")
    rows = run.q("""MATCH (:Session {id: $sid})-[:ASKED]->(:Query)-[:ACCESSED]->(e:PEP) WHERE NOT e.number IN $peps
                    WITH DISTINCT e LIMIT 10
                    MATCH (c:PEP) WHERE c.number IN $peps
                    CALL algo.SPpaths({sourceNode: c, targetNode: e, relTypes: $rels, weightProp: 'weight',
                                       maxLen: 3, relDirection: 'both', pathCount: 1})
                    YIELD path, pathWeight
                    RETURN path, pathWeight, c.number, e.number ORDER BY pathWeight LIMIT 1""",
                 {"sid": session, "peps": peps, "rels": CONNECT_RELS})
    if not rows:
        return envelope("session_link", args, run, "not_in_graph",
                        note="Nothing asked earlier in this session is within 3 hops of this answer.")
    path, cost, now, earlier = rows[0]
    run.say(f"Session memory: PEPs this session asked about before, then algo.SPpaths from PEP {now} to the nearest "
            f"(PEP {earlier}, {len(path.edges())} hop(s), cost {cost:.2f})")
    return envelope("session_link", args, run, "answered",
                    rows=[{"now": now, "earlier": earlier, "hops": len(path.edges()), "cost": round(cost, 2)}],
                    paths=[path_view(path)])


def my_session(session: str = "", tenant: str = "python") -> dict:
    """What was asked earlier in this session, in order, with the PEPs each answer touched."""
    run, args = Run(tenant), {"session": session}
    rows = run.q("""MATCH (:Session {id: $sid})-[:ASKED]->(q:Query)
                    OPTIONAL MATCH (q)-[:ACCESSED]->(p:PEP)
                    RETURN q.text, q.at, collect(p.number) ORDER BY q.at""", {"sid": session}) \
        if SESSION_ID.fullmatch(session or "") else []
    if not rows:
        return envelope("my_session", args, run, "not_in_graph", note="Nothing has been asked in this session yet.")
    run.say(f"Read this session's Query nodes from the graph: {len(rows)} earlier question(s)")
    paths = [path_view(r[0]) for r in run.q("""MATCH path = (:Session {id: $sid})-[:ASKED]->(:Query)-[:ACCESSED]->(:PEP)
                                                RETURN path LIMIT 12""", {"sid": session})]
    return envelope("my_session", args, run, "answered", paths=paths,
                    rows=[{"question": t, "at": a, "peps": sorted(set(p))} for t, a, p in rows])


KIND_ORDER = {"pep": 0, "alt": 1, "thread": 2}


def thread_context(run: Run, tid: int) -> tuple[dict, list]:
    """A discuss.python.org thread: who started it, who took part (committers flagged), and the
    PEPs it discusses or mentions with their status."""
    (t, starter, people, peps, paths, who), = run.q("""
        MATCH (t:Thread {id: $id})
        OPTIONAL MATCH (s:Person)-[:STARTED]->(t)
        WITH t, s.name AS starter
        OPTIONAL MATCH (a:Person)-[e:POSTED_IN]->(t)
        WITH t, starter, a, e ORDER BY e.posts DESC
        WITH t, starter, collect({name: a.name, posts: e.posts, group: a.forum_group})[0..6] AS people
        OPTIONAL MATCH path = (t)-[r:DISCUSSES|MENTIONS]->(p:PEP)
        WITH t, starter, people, collect({pep: p.number, title: p.title, status: p.status, how: type(r)}) AS peps,
             collect(path) AS paths
        // Who started it and the committers who posted: drawn even when the thread names no PEP.
        OPTIONAL MATCH who = (a:Person)-[e:STARTED|POSTED_IN]->(t)
        WHERE type(e) = 'STARTED' OR a.forum_group = 'committers'
        RETURN t, starter, people, peps, paths, collect(who)[0..5]""", {"id": tid})
    tp = t.properties
    row = {"pep": None, "thread_id": tid, "title": tp["title"], "url": tp["url"], "created": tp.get("created"),
           "posts": tp.get("posts"), "likes": tp.get("likes"), "quote": first_sentences(tp.get("text") or ""),
           "started_by": starter, "participants": [x for x in people if x["name"]],
           "peps": [x for x in peps if x["pep"] is not None]}
    return row, [path_view(p) for p in (paths or []) + (who or [])]


def already_decided(idea: str, tenant: str = "python") -> dict:
    """Has Python already decided this idea? Finds the PEP or the rejected idea inside a PEP that
    matches the proposal, then follows the graph: the ruling and who made it, whether it was
    superseded, which later PEPs set the same approach aside again, and who to ask. Use for
    "has X been proposed", "why not X", "was X rejected", or any pasted proposal."""
    run, args = Run(tenant), {"idea": idea}
    idea = idea_text(idea)
    if idea != args["idea"]:
        args["searched_for"] = idea
    cands = [c for c in idea_candidates(run, idea) if c["score"] >= RELATED_IDEA]
    # A rejected idea inside a PEP that itself matched is a side discussion of that PEP, not a second answer.
    peps = {c["key"] for c in cands if c["kind"] == "pep"}
    cands = [c for c in cands if c["kind"] != "alt" or int(c["key"].split("pep-")[1][:4]) not in peps]
    # Among "same idea" matches, a PEP whose subject is the idea outranks an idea rejected inside
    # some other PEP ("type annotations for variables": PEP 526 over a sub-idea of PEP 622), and
    # both outrank a forum thread: a thread is where an idea was discussed, not decided.
    band = lambda c: 0 if c["score"] >= SAME_IDEA else 1
    hits = sorted(cands, key=lambda c: (band(c), KIND_ORDER[c["kind"]], -c["score"]))[:3]
    if not hits:
        return envelope("already_decided", args, run, "not_in_graph",
                        note=f"No PEP, rejected idea or discuss.python.org thread in the graph matches {idea!r} "
                             "closely. That is not proof the idea is new.")
    rows, paths = [], []
    for h in hits:
        match = "same" if h["score"] >= SAME_IDEA else "related"
        if h["kind"] == "pep":
            row, ps = pep_context(run, h["key"])
            rows.append({"kind": "pep", "match": match, "score": h["score"], **row})
            run.say(f"Graph traversal from PEP {row['pep']}: ruling {'found' if row['decision'] else 'not recorded'}, "
                    f"ruling notice {'quoted' if row['reason'] else 'none'}, "
                    f"superseded {'by ' + row['superseded_by'] if row['superseded_by'] else 'no'}, "
                    f"{len(row['came_back'])} later PEP(s) set its approach aside (REJECTS_APPROACH_OF)")
        elif h["kind"] == "thread":
            row, ps = thread_context(run, h["key"])
            rows.append({"kind": "thread", "match": match, "score": h["score"], **row})
            run.say(f"Graph traversal from forum thread {row['thread_id']}: started by {row['started_by']}, "
                    f"{len(row['participants'])} top participants, {len(row['peps'])} linked PEP(s)")
        else:
            (alt, pep_n, text, earlier), = run.q("""
                MATCH alt = (a:Alternative {url: $url})-[:REJECTED_IN]->(p:PEP)
                OPTIONAL MATCH (a)-[c:CITES]->(q:PEP)
                RETURN alt, p.number, a.text, collect({pep: q.number, status: q.status, quote: c.quote})""",
                {"url": h["key"]})
            row, ps = pep_context(run, pep_n)
            run.say(f"Graph traversal from rejected idea {h['title'][:50]!r} -[REJECTED_IN]-> PEP {pep_n}, "
                    f"then that PEP's ruling and history")
            rows.append({"kind": "rejected_idea", "match": match, "score": h["score"], "idea": h["title"],
                         "idea_url": h["key"], "idea_quote": first_sentences(text),
                         "earlier_forms": [e for e in earlier if e["pep"]], **row})
            ps = [path_view(alt)] + ps
        paths += ps
    return envelope("already_decided", args, run, "answered", rows=rows, paths=paths)


def draft_reply(idea: str, tenant: str = "python") -> dict:
    """Draft a forum reply to someone proposing `idea`: what was decided before, quoted, with
    links and who to ask. Built only from already_decided's rows; nothing is posted."""
    res = already_decided(idea, tenant)
    res["tool"], res["args"] = "draft_reply", {"idea": idea}
    same = [r for r in res["rows"] if r["match"] == "same"]
    if not same:
        res["note"] = (res["note"] or "Only loosely related history was found, so there is no reply to draft.")
        return res
    lines = ["This has come up before."]
    for r in same:
        lines.append("")
        if r["kind"] == "thread":
            core = [x["name"] for x in r["participants"] if x["group"] == "committers"][:3]
            lines.append(f'- It was discussed on discuss.python.org: "{r["title"]}" ({(r["created"] or "")[:4]}, '
                         f'{r["posts"]} posts) {r["url"]}' + (f". Core developers in that thread: {', '.join(core)}." if core else ""))
            for x in r["peps"][:2]:
                lines.append(f"  The thread {'is the discussion of' if x['how'] == 'DISCUSSES' else 'refers to'} "
                             f"PEP {x['pep']} ({x['title']}, {x['status']}).")
            continue
        if r["kind"] == "rejected_idea":
            lines.append(f'- PEP {r["pep"]} ("{r["title"]}", {r["created"][:4]}) considered "{r["idea"]}" and set it aside: '
                         f'"{r["idea_quote"]}" {r["idea_url"]}')
            for e in r["earlier_forms"][:2]:
                if e["quote"]:
                    lines.append(f'  It points back to PEP {e["pep"]} ({e["status"]}): "{e["quote"]}"')
        else:
            d = r["decision"]
            by = ""
            if d:
                names = " and ".join(x["by"] for x in d["decided_by"])
                by = f", {'attributed to' if any(x['inferred'] for x in d['decided_by']) else 'decided by'} {names}"
            lines.append(f'- PEP {r["pep"]} ("{r["title"]}", {r["created"][:4]}) proposed it. Status: {r["verdict"]}{by}. '
                         f'From the PEP: "{r["quote"]}" {r["url"]}')
            if r["reason"]:
                lines.append(f'  Its {r["reason"]["section"]}: "{r["reason"]["quote"]}" {r["reason"]["url"]}')
            if d and d["url"]:
                lines.append(f"  The ruling: {d['url']}")
        for b in r["came_back"][:3]:
            said = f' "{b["quote"]}"' if b.get("quote") else ""
            lines.append(f'  It came up again in PEP {b["pep"]} ({(b["created"] or "")[:4]}), which set it aside:{said} {b["url"]}')
        if r["superseded_by"]:
            lines.append(f"  PEP {r['pep']} was later superseded by {r['superseded_by']}.")
    people = sorted({a for r in same for a in r.get("authors", [])})[:4]
    advice = ("A new proposal should start from these threads and what was said there."
              if all(r["kind"] == "thread" for r in same) else "A new proposal would need to answer the reasons above.")
    lines += ["", advice
              + (f" People who know this history: {', '.join(people)}." if people else ""),
              "", "(Drafted by Lineage from the PEP graph. Check the links before posting.)"]
    res["rows"] = [{"draft": "\n".join(lines)}] + res["rows"]
    return res


TOOLS = {f.__name__: f for f in (resolve, why, is_current, who_knows, bus_factor, connection, recall, remember,
                                 already_decided, draft_reply, changes, my_session)}


def call(name: str, args: dict, tenant: str = "python") -> dict:
    t0 = time.perf_counter()
    if name not in TOOLS:
        return {"tool": name, "args": args, "outcome": "error", "note": f"Unknown tool {name}", "rows": [], "paths": []}
    try:
        # Check arguments against the signature first, so a TypeError raised inside a tool is a
        # bug that surfaces, not something reported back as "bad arguments".
        inspect.signature(TOOLS[name]).bind(**args, tenant=tenant)
    except TypeError as e:
        return {"tool": name, "args": args, "outcome": "error", "note": f"Bad arguments: {e}", "rows": [], "paths": [],
                "ms": round((time.perf_counter() - t0) * 1000, 1)}
    try:
        res = TOOLS[name](**args, tenant=tenant)
    except ResponseError as e:
        if "empty key" not in str(e):
            raise
        res = {"tool": name, "args": args, "outcome": "unknown_entity", "rows": [], "paths": [],
               "note": f"Tenant {tenant!r} has no graph yet."}
    res["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return res
