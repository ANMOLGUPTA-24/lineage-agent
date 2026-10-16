"""Load PEP metadata (peps.python.org API) and PEP sources (python/peps repo) into the graph.

Inputs (both public):
  data/raw/peps.json   https://peps.python.org/api/peps.json
  data/raw/peps/       git clone --depth 1 https://github.com/python/peps
"""
import json
import re
from datetime import datetime
from pathlib import Path

from lineage.db import graph

RAW = Path(__file__).resolve().parents[2] / "data" / "raw"

# Python's governance changed on 2018-07-12 (Guido stepped down as BDFL); the first
# Steering Council took office 2019-02-04. We use this to attribute decisions that
# have no explicit delegate. These edges are marked inferred=true.
BDFL_END = datetime(2018, 7, 12)

HEADER = re.compile(r"^([A-Za-z-]+):\s*(.*)$")
PEP_MENTION = re.compile(r"(?::pep:`(?:[^`<]*<)?(\d{1,4})(?:#[^`>]*)?>?`|\bPEP\s?(\d{1,4})\b)")
EMAIL = re.compile(r"\s*<[^>]*>|\s*\([^)]*\)")


def parse_rst(path: Path) -> dict:
    """Headers (with continuation lines), abstract text, and PEP numbers mentioned in the body."""
    text = path.read_text(encoding="utf-8", errors="replace")
    head, _, body = text.partition("\n\n")
    headers, key = {}, None
    for line in head.splitlines():
        m = HEADER.match(line)
        if m:
            key = m.group(1)
            headers[key] = m.group(2).strip()
        elif key and line.startswith((" ", "\t")):
            headers[key] += " " + line.strip()
    abstract = ""
    m = re.search(r"^Abstract\n=+\n(.*?)(?:\n\S[^\n]*\n=+\n)", body, re.S | re.M)
    if m:
        abstract = " ".join(m.group(1).split())[:1500]
    mentions = {int(a or b) for a, b in PEP_MENTION.findall(body)}
    return {"headers": headers, "abstract": abstract, "mentions": mentions}


def names(field: str | None) -> list[str]:
    if not field:
        return []
    cleaned = EMAIL.sub("", field)
    return [n.strip() for n in cleaned.split(",") if n.strip()]


def numbers(field: str | None) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", field or "")]


def build_rows() -> dict[str, list]:
    meta = json.loads((RAW / "peps.json").read_text())
    peps, authored, delegates, rels, releases, topics, decisions = [], [], [], [], [], [], []
    known = {int(k) for k in meta}

    for key, p in meta.items():
        num = int(key)
        src = RAW / "peps" / "peps" / f"pep-{num:04d}.rst"
        parsed = parse_rst(src) if src.exists() else {"headers": {}, "abstract": "", "mentions": set()}
        h = parsed["headers"]
        created = datetime.strptime(p["created"], "%d-%b-%Y") if p.get("created") else None
        peps.append({
            "number": num, "title": p["title"], "status": p["status"], "type": p["type"],
            "created": created.date().isoformat() if created else None,
            "python_version": p.get("python_version") or None,
            "url": p["url"], "abstract": parsed["abstract"],
        })
        for n in p.get("author_names") or []:
            authored.append({"name": n.strip(), "pep": num, "role": "author"})
        for n in names(h.get("Sponsor")):
            authored.append({"name": n, "pep": num, "role": "sponsor"})
        delegate = names(h.get("PEP-Delegate") or h.get("BDFL-Delegate"))
        for n in delegate:
            delegates.append({"name": n, "pep": num})

        for t in numbers(p.get("superseded_by")):
            rels.append({"src": t, "dst": num, "type": "SUPERSEDES"})
        for t in numbers(p.get("replaces")):
            rels.append({"src": num, "dst": t, "type": "SUPERSEDES"})
        for t in numbers(p.get("requires")):
            rels.append({"src": num, "dst": t, "type": "REQUIRES"})
        for t in parsed["mentions"] - {num}:
            if t in known:
                rels.append({"src": num, "dst": t, "type": "REFERENCES"})

        for v in re.split(r",\s*", p.get("python_version") or ""):
            if v:
                releases.append({"pep": num, "version": v.strip()})
        for t in re.split(r",\s*", p.get("topic") or ""):
            if t:
                topics.append({"pep": num, "topic": t.strip().lower()})

        if p.get("resolution") and p["status"] in {"Final", "Accepted", "Active", "Rejected", "Superseded", "Provisional"}:
            outcome = "rejected" if p["status"] == "Rejected" else "accepted"
            if delegate:
                deciders, inferred = [(n, "Person") for n in delegate], False
            elif created and created < BDFL_END:
                deciders, inferred = [("Guido van Rossum", "Person")], True
            else:
                deciders, inferred = [("Python Steering Council", "Council")], True
            for name, kind in deciders:
                decisions.append({"pep": num, "url": p["resolution"], "outcome": outcome,
                                  "by": name, "kind": kind, "inferred": inferred})

    rels = [dict(t) for t in {tuple(sorted(r.items())) for r in rels}]
    return {"peps": peps, "authored": authored, "delegates": delegates, "rels": rels,
            "releases": releases, "topics": topics, "decisions": decisions}


def load(g, rows: dict[str, list]) -> None:
    g.query("UNWIND $rows AS r MERGE (p:PEP {number: r.number}) SET p += r", {"rows": rows["peps"]})
    g.query("""UNWIND $rows AS r MATCH (p:PEP {number: r.pep}) MERGE (a:Person {name: r.name})
               MERGE (a)-[e:AUTHORED]->(p) SET e.role = r.role, e.weight = 1.0""", {"rows": rows["authored"]})
    g.query("""UNWIND $rows AS r MATCH (p:PEP {number: r.pep}) MERGE (a:Person {name: r.name})
               MERGE (a)-[e:DELEGATE_FOR]->(p) SET e.weight = 1.0""", {"rows": rows["delegates"]})
    for t in ("SUPERSEDES", "REQUIRES", "REFERENCES"):
        g.query(f"""UNWIND $rows AS r MATCH (a:PEP {{number: r.src}}), (b:PEP {{number: r.dst}})
                    MERGE (a)-[e:{t}]->(b) SET e.weight = 1.0""",
                {"rows": [r for r in rows["rels"] if r["type"] == t]})
    g.query("""UNWIND $rows AS r MATCH (p:PEP {number: r.pep}) MERGE (v:Release {version: r.version})
               MERGE (p)-[e:TARGETS]->(v) SET e.weight = 1.0""", {"rows": rows["releases"]})
    g.query("""UNWIND $rows AS r MATCH (p:PEP {number: r.pep}) MERGE (t:Topic {name: r.topic})
               MERGE (p)-[e:IN_TOPIC]->(t) SET e.weight = 1.0""", {"rows": rows["topics"]})
    g.query("""UNWIND $rows AS r MATCH (p:PEP {number: r.pep})
               MERGE (d:Decision {pep: r.pep}) SET d.url = r.url, d.outcome = r.outcome
               MERGE (p)-[e:RESOLVED_BY]->(d) SET e.weight = 1.0""", {"rows": rows["decisions"]})
    for kind in ("Person", "Council"):
        g.query(f"""UNWIND $rows AS r MATCH (d:Decision {{pep: r.pep}}) MERGE (x:{kind} {{name: r.by}})
                    MERGE (d)-[e:MADE_BY]->(x) SET e.inferred = r.inferred, e.weight = 1.0""",
                {"rows": [r for r in rows["decisions"] if r["kind"] == kind]})


def create_indexes(g) -> None:
    for stmt in ("CREATE INDEX FOR (p:PEP) ON (p.number)",
                 "CREATE INDEX FOR (p:Person) ON (p.name)",
                 "CREATE INDEX FOR (t:Topic) ON (t.name)",
                 "CREATE INDEX FOR (d:Decision) ON (d.pep)",
                 "CREATE INDEX FOR (r:Release) ON (r.version)"):
        try:
            g.query(stmt)
        except Exception as e:  # already exists
            if "already" not in str(e).lower():
                raise


def main() -> None:
    g = graph()
    create_indexes(g)
    rows = build_rows()
    load(g, rows)
    print({k: len(v) for k, v in rows.items()})


if __name__ == "__main__":
    main()
