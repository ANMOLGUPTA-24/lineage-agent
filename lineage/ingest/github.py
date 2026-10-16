"""Steering Council decisions and CPython PRs/issues that mention PEPs, from public GitHub data.

Fetching uses the `gh` CLI (authenticated, so rate limits are generous) and caches raw JSON in
data/raw/, so loading the graph never needs the network.

  uv run python -m lineage.ingest.github fetch   # slow, about 10 min (search API is 30 req/min)
  uv run python -m lineage.ingest.github load
"""
import json
import re
import subprocess
import sys
import time

from lineage.db import graph
from lineage.ingest.peps import RAW

COUNCIL = RAW / "council.json"
CPYTHON = RAW / "cpython.json"
USERS = RAW / "users.json"
PEP_IN_TEXT = re.compile(r"\bPEP[\s-]?(\d{1,4})\b", re.I)
BOTS = {"miss-islington", "bedevere-bot", "dependabot[bot]", "github-actions[bot]"}


def gh(path: str, **params) -> object:
    args = ["gh", "api", "-X", "GET", path] + [f"-f{k}={v}" for k, v in params.items()]
    return json.loads(subprocess.run(args, check=True, capture_output=True, text=True).stdout)


def fetch_council() -> None:
    issues, page = [], 1
    while True:
        batch = gh("repos/python/steering-council/issues", state="all", labels="PEP", per_page=100, page=page)
        if not batch:
            break
        issues += batch
        page += 1
    out = []
    for i in issues:
        peps = sorted({int(n) for n in PEP_IN_TEXT.findall(i["title"])})
        if not peps or i["state"] != "closed":
            continue
        comments = gh(f"repos/python/steering-council/issues/{i['number']}/comments", per_page=100)
        last = comments[-1] if comments else None
        out.append({
            "number": i["number"], "title": i["title"], "url": i["html_url"], "peps": peps,
            "closed_at": i["closed_at"], "opened_by": i["user"]["login"],
            "verdict": (last["body"][:1200] if last else ""),
            "verdict_by": (last["user"]["login"] if last else None),
            "verdict_url": (last["html_url"] if last else i["html_url"]),
        })
    COUNCIL.write_text(json.dumps(out, indent=1))
    print("council decisions:", len(out))


def target_peps() -> list[int]:
    g = graph()
    rows = g.ro_query("""MATCH (p:PEP) WHERE p.type = 'Standards Track'
                         AND p.status IN ['Final', 'Accepted', 'Provisional'] AND p.created >= '2012-01-01'
                         RETURN p.number ORDER BY p.number""").result_set
    return [r[0] for r in rows]


def fetch_cpython() -> None:
    cache = json.loads(CPYTHON.read_text()) if CPYTHON.exists() else {}
    for num in target_peps():
        if str(num) in cache:
            continue
        res = gh("search/issues", q=f'repo:python/cpython in:title "PEP {num}"', per_page=50, sort="comments")
        items = []
        for it in res["items"]:
            title = it["title"]
            if it["user"]["login"] in BOTS or title.startswith("[3.") or num not in {int(n) for n in PEP_IN_TEXT.findall(title)}:
                continue
            is_pr = "pull_request" in it
            items.append({
                "number": it["number"], "title": title, "url": it["html_url"], "kind": "PR" if is_pr else "Issue",
                "author": it["user"]["login"], "created_at": it["created_at"], "closed_at": it["closed_at"],
                "merged": bool(is_pr and it["pull_request"].get("merged_at")), "comments": it["comments"],
            })
        cache[str(num)] = items
        CPYTHON.write_text(json.dumps(cache, indent=1))
        print(num, len(items), flush=True)
        time.sleep(2.2)  # search API: 30 requests/minute


def fetch_users() -> None:
    """GitHub display names, so PR authors join PEP authors (who are listed by real name)."""
    users = json.loads(USERS.read_text()) if USERS.exists() else {}
    logins = {it["author"] for its in json.loads(CPYTHON.read_text()).values() for it in its}
    for login in sorted(logins - users.keys()):
        try:
            users[login] = gh(f"users/{login}").get("name")
        except subprocess.CalledProcessError:
            users[login] = None
    USERS.write_text(json.dumps(users, indent=1))
    print("users:", len(users))


def load() -> None:
    g = graph()
    for stmt in ("CREATE INDEX FOR (p:PR) ON (p.number)", "CREATE INDEX FOR (i:Issue) ON (i.number)",
                 "CREATE INDEX FOR (p:Person) ON (p.github)"):
        try:
            g.query(stmt)
        except Exception as e:
            if "already" not in str(e).lower():
                raise

    council = json.loads(COUNCIL.read_text())
    rows = [{"pep": p, **{k: c[k] for k in ("number", "title", "url", "closed_at", "verdict", "verdict_url")}}
            for c in council for p in c["peps"]]
    # A real Steering Council ruling replaces the inferred attribution for that PEP.
    g.query("""UNWIND $rows AS r MATCH (p:PEP {number: r.pep})
               MERGE (d:Decision {pep: r.pep})
               SET d.council_issue = r.url, d.at = r.closed_at, d.verdict = r.verdict, d.verdict_url = r.verdict_url,
                   d.url = coalesce(d.url, r.verdict_url), d.outcome = coalesce(d.outcome, CASE p.status
                       WHEN 'Rejected' THEN 'rejected' WHEN 'Withdrawn' THEN 'withdrawn' ELSE 'accepted' END)
               MERGE (p)-[e:RESOLVED_BY]->(d) SET e.weight = 1.0
               WITH d
               OPTIONAL MATCH (d)-[old:MADE_BY {inferred: true}]->() DELETE old
               WITH d MERGE (c:Council {name: 'Python Steering Council'})
               MERGE (d)-[m:MADE_BY]->(c) SET m.inferred = false, m.weight = 1.0""", {"rows": rows})

    cpython = json.loads(CPYTHON.read_text())
    users = json.loads(USERS.read_text()) if USERS.exists() else {}
    for kind, rel in (("PR", "IMPLEMENTS"), ("Issue", "DISCUSSES")):
        items = [{"pep": int(p), **it, "name": (users.get(it["author"]) or it["author"]).strip()}
                 for p, its in cpython.items() for it in its if it["kind"] == kind]
        g.query(f"""UNWIND $rows AS r MATCH (p:PEP {{number: r.pep}})
                    MERGE (x:{kind} {{number: r.number}})
                    SET x.title = r.title, x.url = r.url, x.created_at = r.created_at,
                        x.closed_at = r.closed_at, x.merged = r.merged, x.comments = r.comments
                    MERGE (x)-[e:{rel}]->(p) SET e.weight = 1.0
                    MERGE (a:Person {{name: r.name}}) SET a.github = r.author
                    MERGE (a)-[o:OPENED]->(x) SET o.weight = 1.0""", {"rows": items})
        print(kind, len(items))
    print("council rows:", len(rows))


if __name__ == "__main__":
    {"fetch": lambda: (fetch_council(), fetch_cpython(), fetch_users()), "load": load}[sys.argv[1]]()
