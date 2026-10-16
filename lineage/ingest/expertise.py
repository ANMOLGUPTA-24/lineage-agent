"""Expertise per area: whose work the area builds on, and what happens if they leave.

Scopes are the official Topics (typing, packaging, release, governance) and every
label-propagation Community of MIN_COMMUNITY or more PEPs. For each scope we project a
Person -> Person graph and run PageRank on it:

  (a)-[:INFLUENCE_<scope>]->(b)   for a PEP q in the scope that b authored or decided, when
      a authored a PEP that REQUIRES / REFERENCES / SUPERSEDES q, or
      a opened a PR or issue that IMPLEMENTS / DISCUSSES q, or
      a authored q and b was its delegate (deciders gain authority from the work they ruled on)

FalkorDB's algo.pageRank takes one label and one relationship type and ignores multiplicity
(tested: three parallel a->b edges score the same as one, and there is no weight argument), so
the projection has one edge per pair; the evidence count is kept as `n` for the explanation.

Bus factor: algo.WCC over the same projection, with and without the scope's top person (a
temporary copy of the edges minus theirs), gives the size of the largest connected group of
people both ways.

Written here, at ingest:
  (:Person)-[:EXPERT_IN {rank, share, items}]->(:Topic | :Community)
  scope node {slug, people, top, largest, largest_without_top, groups_without_top}
Request-time tools only read these.
"""
import re

from lineage.db import graph

MIN_COMMUNITY = 8
# Author strings that are groups, not people ("The Python core team and community").
NOT_A_PERSON = re.compile(r"\b(team|community|council|developers|contributors)\b", re.I)
EVIDENCE = "REQUIRES|REFERENCES|SUPERSEDES|IMPLEMENTS|DISCUSSES"


def scopes(g) -> list[dict]:
    topics = [{"kind": "Topic", "key": n, "slug": n} for n, in g.ro_query("MATCH (t:Topic) RETURN t.name").result_set]
    comms = [{"kind": "Community", "key": c, "slug": f"c{c}"} for c, in g.ro_query(
        "MATCH (c:Community) WHERE c.size >= $m RETURN c.id", {"m": MIN_COMMUNITY}).result_set]
    return topics + comms


def scope_match(kind: str) -> str:
    # Labels and relationship types can't be query parameters; `kind` is one of two literals.
    return {"Topic": "MATCH (q:PEP)-[:IN_TOPIC]->(s:Topic {name: $key})",
            "Community": "MATCH (q:PEP)-[:IN_COMMUNITY]->(s:Community {id: $key})"}[kind]


def scope_node(kind: str) -> str:
    return {"Topic": "MATCH (s:Topic {name: $key})", "Community": "MATCH (s:Community {id: $key})"}[kind]


def components(g, rel: str, people: list[int]) -> list[int]:
    """Sizes of the connected groups that `people` (node ids) form over `rel`, largest first."""
    rows = g.ro_query(f"""CALL algo.WCC({{nodeLabels: ['Person'], relationshipTypes: ['{rel}']}})
                          YIELD node, componentId WITH node, componentId WHERE id(node) IN $ids
                          RETURN componentId, count(*) AS size ORDER BY size DESC""", {"ids": people}).result_set
    return [size for _, size in rows]


def project(g, sc: dict) -> dict:
    rel, match, key = f"INFLUENCE_{sc['slug']}", scope_match(sc["kind"]), sc["key"]
    g.query(f"MATCH ()-[e:{rel}]->() DELETE e")
    g.query(f"""{match}
                MATCH (b:Person)-[:AUTHORED|DELEGATE_FOR]->(q)
                MATCH (a:Person)-[:AUTHORED|OPENED]->(x)-[:{EVIDENCE}]->(q)
                WHERE a <> b
                WITH a, b, count(*) AS n
                CREATE (a)-[:{rel} {{n: n}}]->(b)""", {"key": key})
    g.query(f"""{match}
                MATCH (a:Person)-[:AUTHORED]->(q)<-[:DELEGATE_FOR]-(b:Person) WHERE a <> b
                WITH a, b, count(*) AS n
                MERGE (a)-[e:{rel}]->(b) ON CREATE SET e.n = n ON MATCH SET e.n = e.n + n""", {"key": key})

    # People involved in the scope, and how many of its PEPs / PRs each touched.
    touched: dict[int, set] = {}
    for p, name, x in g.ro_query(f"""{match} MATCH (p:Person)-[:AUTHORED|DELEGATE_FOR]->(q)
                                     RETURN id(p) AS p, p.name AS name, id(q) AS x
                                     UNION ALL
                                     {match} MATCH (p:Person)-[:OPENED]->(x)-[:IMPLEMENTS|DISCUSSES]->(q)
                                     RETURN id(p) AS p, p.name AS name, id(x) AS x""", {"key": key}).result_set:
        if not NOT_A_PERSON.search(name or ""):
            touched.setdefault(p, set()).add(x)
    involved = {p: len(xs) for p, xs in touched.items()}
    if not involved:
        return {"slug": sc["slug"], "people": 0}

    pr = g.ro_query(f"CALL algo.pageRank('Person', '{rel}') YIELD node, score RETURN id(node), score").result_set
    scores = {i: s for i, s in pr if i in involved}
    total = sum(scores.values()) or 1.0
    ranked = sorted(involved, key=lambda i: (-scores.get(i, 0.0), -involved[i]))
    rows = [{"id": i, "rank": r + 1, "share": round(scores.get(i, 0.0) / total, 4), "items": involved[i]}
            for r, i in enumerate(ranked)]
    g.query(f"""MATCH ()-[e:EXPERT_IN {{scope: $slug}}]->() DELETE e""", {"slug": sc["slug"]})
    g.query(f"""UNWIND $rows AS r MATCH (p:Person) WHERE id(p) = r.id
                {scope_node(sc['kind'])}
                CREATE (p)-[:EXPERT_IN {{scope: $slug, rank: r.rank, share: r.share, items: r.items, weight: 1.0}}]->(s)""",
            {"rows": rows, "key": key, "slug": sc["slug"]})

    top, people = ranked[0], list(involved)
    with_top = components(g, rel, people)
    g.query(f"MATCH (a)-[e:{rel}]->(b) WHERE id(a) <> $top AND id(b) <> $top CREATE (a)-[:{rel}_WO]->(b)", {"top": top})
    without = components(g, f"{rel}_WO", [p for p in people if p != top])
    g.query(f"MATCH ()-[e:{rel}_WO]->() DELETE e")
    stats = {"slug": sc["slug"], "people": len(people), "top": top, "largest": with_top[0] if with_top else 0,
             "largest_without_top": without[0] if without else 0,
             "groups_without_top": sum(1 for s in without if s >= 2)}
    g.query(f"""{scope_node(sc['kind'])}
                MATCH (t:Person) WHERE id(t) = $top
                SET s.slug = $slug, s.people = $people, s.top = t.name, s.largest = $largest,
                    s.largest_without_top = $largest_without_top, s.groups_without_top = $groups_without_top""",
            {"key": key, **stats})
    return stats


def main() -> None:
    g = graph()
    for sc in scopes(g):
        print(project(g, sc))


if __name__ == "__main__":
    main()
