"""discuss.python.org threads: where ideas are proposed, and where most of them stop.

  python -m lineage.ingest.discourse fetch    # public JSON API -> data/raw/discourse.json (cached, incremental)
  python -m lineage.ingest.discourse load     # cache -> graph

Sources (both public; robots.txt allows /c/ and /t/, and /search is not used):
  /c/ideas/6.json?page=N   every topic in the Ideas category
  /t/<id>.json             first post and participants, for Ideas topics with MIN_POSTS+ posts
                           and for every PEP whose `discussions_to` is a discuss.python.org thread

Graph:
  (:Thread {id, title, url, created, posts, likes, views, kind: 'idea' | 'pep', text})
  (:Thread)-[:DISCUSSES]->(:PEP)                from the PEP's Discussions-To header
  (:Thread)-[:MENTIONS]->(:PEP)                 "PEP NNN" in the title or first post
  (:Person)-[:STARTED]->(:Thread)
  (:Person)-[:POSTED_IN {posts}]->(:Thread)     top participants; matched to existing people by
                                                display name, created otherwise ({forum: username})
"""
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request

from fastembed import TextEmbedding

from lineage.db import graph
from lineage.ingest.derive import EMBED_MODEL, try_query
from lineage.ingest.peps import RAW
from lineage.ingest.rst import clip

BASE = "https://discuss.python.org"
CACHE = RAW / "discourse.json"
UA = "lineage-graph-hacks/1.0 (+https://github.com/ANMOLGUPTA-24/lineage-agent)"
MIN_POSTS = 5
MAX_PAGES = 250
DELAY = 0.5
PEP_IN_TEXT = re.compile(r"\bPEP[\s-]?0*(\d{1,4})\b", re.I)


def get(path: str) -> dict | None:
    for attempt in range(5):
        try:
            req = urllib.request.Request(BASE + path, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                time.sleep(DELAY)
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After") or 10) + 1)
                continue
            raise
        except urllib.error.URLError:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"gave up on {path}")


def text_of(cooked: str) -> str:
    cooked = re.sub(r"(?s)<aside class=\"(quote|onebox)[^>]*>.*?</aside>", " ", cooked)   # quoted posts, link cards
    cooked = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", cooked)
    text = html.unescape(re.sub(r"<[^>]+>", " ", cooked))
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n|\n", text)]
    return "\n\n".join(p for p in paras if p)


def thread_id(url: str) -> int | None:
    m = re.match(r"https?://discuss\.python\.org/t/(?:[^/]+/)?(\d+)", url or "")
    return int(m.group(1)) if m else None


def fetch() -> None:
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {"ideas": {}, "topics": {}}
    for page in range(MAX_PAGES):
        d = get(f"/c/ideas/6.json?page={page}")
        topics = (d or {}).get("topic_list", {}).get("topics", [])
        if not topics:
            break
        names = {u["id"]: u.get("name") or u["username"] for u in d.get("users", [])}
        for t in topics:
            op = next((p["user_id"] for p in t.get("posters", []) if "Original Poster" in (p.get("description") or "")), None)
            cache["ideas"][str(t["id"])] = {"id": t["id"], "title": t["title"], "created": t["created_at"],
                                            "posts": t["posts_count"], "likes": t.get("like_count", 0),
                                            "views": t.get("views", 0), "op": names.get(op)}
        print(f"page {page}: {len(cache['ideas'])} ideas", flush=True)

    peps = json.loads((RAW / "peps.json").read_text())
    wanted = {i["id"]: "idea" for i in cache["ideas"].values() if i["posts"] >= MIN_POSTS}
    wanted.update({tid: "pep" for p in peps.values() if (tid := thread_id(p.get("discussions_to")))})
    todo = [t for t in wanted if str(t) not in cache["topics"]]
    print(f"{len(wanted)} threads wanted, {len(todo)} to fetch", flush=True)
    for i, tid in enumerate(todo):
        d = get(f"/t/{tid}.json")
        if d:
            posts = d.get("post_stream", {}).get("posts", [])
            first = next((p for p in posts if p.get("post_number") == 1), None)
            cache["topics"][str(tid)] = {
                "id": tid, "kind": wanted[tid], "title": d.get("title"), "created": d.get("created_at"),
                "posts": d.get("posts_count"), "likes": d.get("like_count", 0), "views": d.get("views", 0),
                "op": (d.get("details", {}).get("created_by") or {}).get("name")
                      or (d.get("details", {}).get("created_by") or {}).get("username"),
                "text": clip(text_of(first["cooked"]), 1500) if first else "",
                "participants": [{"name": p.get("name") or p["username"], "username": p["username"],
                                  "posts": p.get("post_count", 0), "group": p.get("primary_group_name")}
                                 for p in d.get("details", {}).get("participants", [])][:15]}
        if i % 50 == 0:
            CACHE.write_text(json.dumps(cache))
            print(f"  {i}/{len(todo)} threads", flush=True)
    CACHE.write_text(json.dumps(cache))
    print({"ideas": len(cache["ideas"]), "threads": len(cache["topics"])})


def load() -> None:
    if not CACHE.exists():
        print("no data/raw/discourse.json; skipping threads (run `python -m lineage.ingest.discourse fetch`)")
        return
    g = graph()
    cache = json.loads(CACHE.read_text())
    peps = json.loads((RAW / "peps.json").read_text())
    pep_of = {}
    for n, p in peps.items():
        if tid := thread_id(p.get("discussions_to")):
            pep_of.setdefault(tid, []).append(int(n))

    rows = []
    for tid, t in cache["topics"].items():
        tid = int(tid)
        mentions = sorted({int(m) for m in PEP_IN_TEXT.findall(f"{t['title']} {t['text']}")} - set(pep_of.get(tid, [])))
        rows.append({"id": tid, "title": t["title"], "url": f"{BASE}/t/{tid}", "created": (t["created"] or "")[:10],
                     "posts": t["posts"] or 0, "likes": t["likes"] or 0, "views": t["views"] or 0, "kind": t["kind"],
                     "text": t["text"], "op": t["op"], "discusses": pep_of.get(tid, []), "mentions": mentions,
                     "people": [p for p in t["participants"] if p["name"]]})

    g.query("MATCH (t:Thread) DETACH DELETE t")
    for i in range(0, len(rows), 200):
        g.query("""UNWIND $rows AS r
                   CREATE (t:Thread {id: r.id, title: r.title, url: r.url, created: r.created, posts: r.posts,
                                     likes: r.likes, views: r.views, kind: r.kind, text: r.text})
                   WITH t, r
                   UNWIND r.discusses AS n MATCH (p:PEP {number: n}) CREATE (t)-[:DISCUSSES {weight: 1.0}]->(p)""",
                {"rows": rows[i:i + 200]})
        g.query("""UNWIND $rows AS r MATCH (t:Thread {id: r.id})
                   UNWIND r.mentions AS n MATCH (p:PEP {number: n}) CREATE (t)-[:MENTIONS {weight: 1.0}]->(p)""",
                {"rows": rows[i:i + 200]})
        g.query("""UNWIND $rows AS r MATCH (t:Thread {id: r.id}) WITH t, r WHERE r.op IS NOT NULL
                   MERGE (a:Person {name: r.op}) CREATE (a)-[:STARTED {weight: 1.0}]->(t)""", {"rows": rows[i:i + 200]})
        g.query("""UNWIND $rows AS r MATCH (t:Thread {id: r.id})
                   UNWIND r.people AS pp
                   MERGE (a:Person {name: pp.name}) ON CREATE SET a.forum = pp.username
                   SET a.forum_group = coalesce(pp.group, a.forum_group)
                   CREATE (a)-[:POSTED_IN {posts: pp.posts, weight: 1.0}]->(t)""", {"rows": rows[i:i + 200]})

    try_query(g, "CREATE INDEX FOR (t:Thread) ON (t.id)")
    try_query(g, "CALL db.idx.fulltext.createNodeIndex('Thread', 'title', 'text')")
    try_query(g, "CREATE VECTOR INDEX FOR (t:Thread) ON (t.embedding) OPTIONS {dimension: 384, similarityFunction: 'cosine'}")
    model = TextEmbedding(EMBED_MODEL)
    vecs = list(model.embed([f"{r['title']}. {r['text'][:600]}" for r in rows]))
    for i in range(0, len(rows), 200):
        g.query("UNWIND $rows AS r MATCH (t:Thread {id: r.id}) SET t.embedding = vecf32(r.v)",
                {"rows": [{"id": r["id"], "v": v.tolist()} for r, v in zip(rows[i:i + 200], vecs[i:i + 200])]})
    print({"threads": len(rows), "idea_threads": sum(r["kind"] == "idea" for r in rows),
           "pep_threads": sum(r["kind"] == "pep" for r in rows),
           "discusses": sum(len(r["discusses"]) for r in rows), "mentions": sum(len(r["mentions"]) for r in rows)})


if __name__ == "__main__":
    {"fetch": fetch, "load": load}[sys.argv[1]]()
