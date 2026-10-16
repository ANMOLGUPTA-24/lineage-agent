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

UNDERLINE = re.compile(r"^([=\-~^\"'`#*+])\1{2,}\s*$")
REJECTED = re.compile(r"^(rejected|alternative|frequently raised objections|objections|why not|"
                      r"open issues and rejected|discarded)", re.I)
RATIONALE = re.compile(r"^(motivation|rationale)\b", re.I)
MAX_TEXT = 900


def demarkup(text: str) -> str:
    text = re.sub(r":pep:`(?:[^`<]*<)?(\d+)(?:#[^`>]*)?>?`", r"PEP \1", text)
    text = re.sub(r":(?:py:)?[a-z]+:`~?([^`<]+?)(?:\s*<[^>]+>)?`", r"\1", text)
    text = re.sub(r"`([^`<]+?)\s*<[^>]+>`_{1,2}", r"\1", text)
    text = re.sub(r"``([^`]+)``", r"`\1`", text)
    text = re.sub(r"\[#?[\w-]*\]_|\[\d+\]_", "", text)
    text = re.sub(r"^\s*\.\. .*$", "", text, flags=re.M)          # directives
    text = re.sub(r"::\s*$", ":", text, flags=re.M)
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
    return "\n\n".join(p for p in paras if p)


def clip(text: str) -> str:
    if len(text) <= MAX_TEXT:
        return text
    cut = text[:MAX_TEXT]
    end = max(cut.rfind(". "), cut.rfind(".\n"))
    return cut[: end + 1] if end > 200 else cut.rstrip() + "…"


def anchor(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", demarkup(title).lower().replace("`", "")).strip("-")


def sections(path: Path) -> list[dict]:
    """Flat list of {level, title, body} using rst's rule: heading levels follow the order in
    which underline characters first appear."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    levels: list[str] = []
    out, cur = [], None
    i = 0
    while i < len(lines):
        if (i + 1 < len(lines) and lines[i].strip() and not lines[i].startswith(" ")
                and UNDERLINE.match(lines[i + 1]) and len(lines[i + 1].rstrip()) >= len(lines[i].rstrip())):
            ch = lines[i + 1][0]
            if ch not in levels:
                levels.append(ch)
            cur = {"level": levels.index(ch), "title": lines[i].strip(), "body": []}
            out.append(cur)
            i += 2
            continue
        if cur is not None:
            cur["body"].append(lines[i])
        i += 1
    for s in out:
        s["body"] = "\n".join(s["body"])
    return out


def extract(num: int, path: Path) -> tuple[list[dict], list[dict]]:
    url = f"https://peps.python.org/pep-{num:04d}/"
    secs = sections(path)
    found_sections, alternatives = [], []
    for idx, s in enumerate(secs):
        if s["level"] != 0:
            continue
        if RATIONALE.match(s["title"]):
            text = demarkup(s["body"])
            if text:
                found_sections.append({"pep": num, "kind": s["title"].split()[0].lower(), "title": s["title"],
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
                alternatives.append({"pep": num, "title": demarkup(c["title"]).replace("`", ""),
                                     "section": s["title"], "text": clip(text),
                                     "url": url + "#" + anchor(c["title"])})
    return found_sections, alternatives


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
    try_query(g, "CALL db.idx.fulltext.createNodeIndex('Alternative', 'title', 'text')")
    try_query(g, "CREATE VECTOR INDEX FOR (a:Alternative) ON (a.embedding) OPTIONS {dimension: 384, similarityFunction: 'cosine'}")
    try_query(g, "CREATE INDEX FOR (s:Section) ON (s.pep)")
    print({"sections": len(secs), "alternatives": len(alts), "peps_with_alternatives": len({a["pep"] for a in alts})})


if __name__ == "__main__":
    main()
