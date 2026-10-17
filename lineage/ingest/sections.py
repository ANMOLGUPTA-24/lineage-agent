"""PEP sections as quotable sources.

  (:PEP)-[:HAS_SECTION]->(:Section {kind, title, text, url})       Motivation / Rationale
  (:Alternative {title, text, url, pep})-[:REJECTED_IN]->(:PEP)     each rejected idea or FAQ objection

Texts are stored lightly de-marked-up so the agent can quote them verbatim, and every node
carries a deep link (peps.python.org/pep-NNNN/#anchor) so a reader can check the quote.
"""
import re
from pathlib import Path

from fastembed import TextEmbedding

from lineage.db import graph
from lineage.ingest.derive import EMBED_MODEL, try_query
from lineage.ingest.peps import RAW
from lineage.ingest.rst import clip, demarkup, sections

REJECTED = re.compile(r"^(rejected|alternative|frequently raised objections|objections|why not|"
                      r"open issues and rejected|discarded)", re.I)
RATIONALE = re.compile(r"^(motivation|rationale)\b", re.I)
# The PEP's own record of its ruling: "Rejection Notice", "PEP Withdrawal", "BDFL Pronouncement"...
NOTICE = re.compile(r"^((pep )?(rejection|withdrawal|deferral)( notice| note)?|(bdfl[- ])?pronouncements?|"
                    r"resolution|(pep )?status|notice)\s*$", re.I)
PEP_MENTION = re.compile(r"\bPEP[\s-]?0*(\d{1,4})\b")


def citing_sentence(text: str, pep: int, limit: int = 400) -> str:
    """The sentence of `text` that mentions `pep`, verbatim; '' when only the title mentions it."""
    for para in text.split("\n\n"):
        for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z(`\"])", para):
            if re.search(rf"\bPEP[\s-]?0*{pep}\b", sent):
                return sent if len(sent) <= limit else sent[:limit].rstrip() + "…"
    return ""


def anchor(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", demarkup(title).lower().replace("`", "")).strip("-")


def extract(num: int, path: Path) -> tuple[list[dict], list[dict]]:
    url = f"https://peps.python.org/pep-{num:04d}/"
    secs = sections(path)
    found_sections, alternatives = [], []
    for idx, s in enumerate(secs):
        if s["level"] != 0:
            continue
        if RATIONALE.match(s["title"]) or NOTICE.match(s["title"]):
            text = demarkup(s["body"])
            if text:
                kind = "notice" if NOTICE.match(s["title"]) else s["title"].split()[0].lower()
                found_sections.append({"pep": num, "kind": kind, "title": s["title"],
                                       "text": clip(text), "url": url + "#" + anchor(s["title"])})
        elif REJECTED.match(s["title"]):
            children = []
            for c in secs[idx + 1:]:
                if c["level"] == 0:
                    break
                if c["level"] == 1:
                    children.append(c)
            intro = demarkup(s["body"])
            if not children and intro:
                children = [{"title": s["title"], "body": s["body"]}]
            for c in children:
                text = demarkup(c["body"])
                if len(text) < 40:
                    continue
                # Citations come from the full text: clip() would drop the ones past MAX_TEXT.
                cited = sorted({int(m) for m in PEP_MENTION.findall(demarkup(c["title"]) + " " + text)} - {num})
                alternatives.append({"pep": num, "title": demarkup(c["title"]).replace("`", ""),
                                     "section": s["title"], "text": clip(text),
                                     "url": url + "#" + anchor(c["title"]),
                                     "cited": [{"pep": q, "quote": citing_sentence(text, q)} for q in cited]})
    return found_sections, alternatives


def link(g, alts: list[dict]) -> None:
    """Edges from what a rejected idea's own text cites.

    Every mention becomes (:Alternative)-[:CITES]->(:PEP). A mention is also read as "this PEP
    rejected the approach of that one" only when the cited PEP is an older competing proposal:
    not a Process PEP and never accepted. In a sample of 210 mentions, ones pointing at Final or
    Accepted PEPs were context ("PEP 484 already covers this"), not earlier proposals, and ones
    pointing at newer PEPs were later edits.
    """
    rows = [{"url": a["url"], "cited": a["cited"]} for a in alts if a["cited"]]
    g.query("MATCH ()-[e:REJECTS_APPROACH_OF]->() DELETE e")
    for i in range(0, len(rows), 300):
        g.query("""UNWIND $rows AS r
                   MATCH (a:Alternative {url: r.url})-[:REJECTED_IN]->(p:PEP)
                   UNWIND r.cited AS c
                   MATCH (q:PEP {number: c.pep}) WHERE q <> p
                   MERGE (a)-[ci:CITES]->(q) SET ci.weight = 1.0, ci.quote = c.quote
                   WITH a, p, q WHERE q.type <> 'Process' AND q.created < p.created
                                  AND q.status IN ['Rejected', 'Withdrawn', 'Deferred', 'Superseded', 'Draft']
                   MERGE (p)-[e:REJECTS_APPROACH_OF]->(q)
                     ON CREATE SET e.weight = 1.0, e.via = [a.url]
                     ON MATCH SET e.via = e.via + a.url""", {"rows": rows[i:i + 300]})


def main() -> None:
    g = graph()
    nums = [r[0] for r in g.ro_query("MATCH (p:PEP) RETURN p.number").result_set]
    secs, alts = [], []
    for n in nums:
        path = RAW / "peps" / "peps" / f"pep-{n:04d}.rst"
        if path.exists():
            s, a = extract(n, path)
            secs += s
            alts += a
    g.query("MATCH (x) WHERE x:Section OR x:Alternative DETACH DELETE x")
    for i in range(0, len(secs), 300):
        g.query("""UNWIND $rows AS r MATCH (p:PEP {number: r.pep})
                   CREATE (p)-[:HAS_SECTION {weight: 1.0}]->(:Section {pep: r.pep, kind: r.kind, title: r.title,
                                                                       text: r.text, url: r.url})""",
                {"rows": secs[i:i + 300]})
    model = TextEmbedding(EMBED_MODEL)
    vecs = list(model.embed([f"{a['title']}. {a['text']}" for a in alts]))
    for a, v in zip(alts, vecs):
        a["v"] = v.tolist()
    for i in range(0, len(alts), 200):
        g.query("""UNWIND $rows AS r MATCH (p:PEP {number: r.pep})
                   CREATE (:Alternative {pep: r.pep, title: r.title, section: r.section, text: r.text,
                                         url: r.url, embedding: vecf32(r.v)})-[:REJECTED_IN {weight: 1.0}]->(p)""",
                {"rows": alts[i:i + 200]})
    link(g, alts)
    try_query(g, "CALL db.idx.fulltext.createNodeIndex('Alternative', 'title', 'text')")
    try_query(g, "CREATE VECTOR INDEX FOR (a:Alternative) ON (a.embedding) OPTIONS {dimension: 384, similarityFunction: 'cosine'}")
    try_query(g, "CREATE INDEX FOR (s:Section) ON (s.pep)")
    try_query(g, "CREATE INDEX FOR (a:Alternative) ON (a.url)")
    print({"sections": len(secs), "alternatives": len(alts), "peps_with_alternatives": len({a["pep"] for a in alts})})


if __name__ == "__main__":
    main()
