"""Derived structure, computed in-graph after the raw loads.

1. Reverse edges (HAS_AUTHOR, OPENED_BY) so centrality treats authorship as two-way.
2. Hub-aware REFERENCES weights: citing PEP 8 says little, so the weight is
   1 + ln(in-degree of target). Shortest paths then avoid routing through style guides.
3. PageRank over the PEP citation graph, stored as p.rank.
4. Communities: label propagation over PEP links. Only 205 PEPs carry an official Topic header,
   so CDLP groups the rest. Each community is named after its highest-ranked PEP.
5. Full-text index (title, abstract) and a vector index on title+abstract embeddings, used
   only to resolve fuzzy names ("walrus operator" -> PEP 572).

Community nodes are rebuilt here, which drops the expertise attached to them, so after running
this module on its own, run lineage.ingest.expertise again (`make ingest` runs both in order).
"""
from fastembed import TextEmbedding

from lineage.db import graph

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
DIM = 384


def try_query(g, q: str) -> None:
    try:
        g.query(q)
    except Exception as e:
        if "already" not in str(e).lower():
            raise


def main() -> None:
    g = graph()
    g.query("MATCH (a:Person)-[:AUTHORED]->(p:PEP) MERGE (p)-[e:HAS_AUTHOR]->(a) SET e.weight = 1.0")
    g.query("MATCH (a:Person)-[:OPENED]->(x) MERGE (x)-[e:OPENED_BY]->(a) SET e.weight = 1.0")

    g.query("""MATCH (t:PEP) OPTIONAL MATCH (:PEP)-[r:REFERENCES]->(t)
               WITH t, count(r) AS indeg
               MATCH (:PEP)-[e:REFERENCES]->(t) SET e.weight = 1.0 + log(1.0 + indeg)""")

    g.query("""CALL algo.pageRank('PEP', 'REFERENCES') YIELD node, score
               SET node.rank = score""")

    # Same idea for people: Guido authored 65 PEPs, so at weight 1 "PEP 484 <- Guido -> PEP 695"
    # (cost 2) beat the direct citation 695 -> 484 (cost 4.3). Each person edge costs
    # 1 + ln(1 + that person's number of edges of that type).
    for rel in ("AUTHORED", "OPENED", "DELEGATE_FOR"):
        g.query(f"""MATCH (a:Person)-[e:{rel}]->() WITH a, count(e) AS n
                    MATCH (a)-[e:{rel}]->() SET e.weight = 1.0 + log(1.0 + n)""")
    # And for topics: "both are typing PEPs" (49 of them) is true of every pair, so it must not
    # undercut a real citation between the two.
    g.query("""MATCH ()-[e:IN_TOPIC]->(t:Topic) WITH t, count(e) AS n
               MATCH ()-[e:IN_TOPIC]->(t) SET e.weight = 1.0 + log(1.0 + n)""")

    # The Steering Council made 100+ rulings; at weight 1 every path would route through it.
    g.query("MATCH (:Decision)-[m:MADE_BY]->(:Council) SET m.weight = 3.0")

    # Bridge score: betweenness over the people <-> PEP/PR collaboration graph. High means the
    # person links otherwise separate groups of work.
    g.query("""CALL algo.betweenness({nodeLabels: ['Person', 'PEP', 'PR'],
                                      relationshipTypes: ['AUTHORED', 'HAS_AUTHOR', 'OPENED', 'OPENED_BY',
                                                          'IMPLEMENTS', 'REFERENCES'],
                                      samplingSize: 2000, samplingSeed: 7})
               YIELD node, score WHERE node:Person SET node.bridge = score""")

    g.query("MATCH (c:Community) DETACH DELETE c")
    g.query("""CALL algo.labelPropagation({nodeLabels: ['PEP'],
                                          relationshipTypes: ['REFERENCES', 'REQUIRES', 'SUPERSEDES']})
               YIELD node, communityId SET node.community = communityId""")
    g.query("""MATCH (p:PEP) WITH p.community AS cid, p ORDER BY p.rank DESC
               WITH cid, collect(p) AS members WHERE size(members) >= 4
               WITH cid, members, members[0] AS hub
               CREATE (c:Community {id: cid, name: hub.title, hub: hub.number, size: size(members)})
               WITH c, members UNWIND members AS m
               CREATE (m)-[:IN_COMMUNITY {weight: 1.0}]->(c)""")

    try_query(g, "CALL db.idx.fulltext.createNodeIndex('PEP', 'title', 'abstract')")
    try_query(g, "CALL db.idx.fulltext.createNodeIndex('Person', 'name')")
    try_query(g, f"CREATE VECTOR INDEX FOR (p:PEP) ON (p.embedding) OPTIONS {{dimension: {DIM}, similarityFunction: 'cosine'}}")

    peps = g.ro_query("MATCH (p:PEP) RETURN p.number, p.title, p.abstract").result_set
    model = TextEmbedding(EMBED_MODEL)
    vecs = list(model.embed([f"PEP {n}: {t}. {a or ''}" for n, t, a in peps]))
    rows = [{"n": n, "v": v.tolist()} for (n, _, _), v in zip(peps, vecs)]
    for i in range(0, len(rows), 200):
        g.query("UNWIND $rows AS r MATCH (p:PEP {number: r.n}) SET p.embedding = vecf32(r.v)", {"rows": rows[i:i + 200]})

    print(g.ro_query("MATCH (c:Community) RETURN c.hub, c.name, c.size ORDER BY c.size DESC LIMIT 10").result_set)


if __name__ == "__main__":
    main()
