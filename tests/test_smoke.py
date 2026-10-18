"""Smoke tests for the HTTP API surface and the static demo GitHub Pages serves from docs/demo/.

The API tests use FastAPI's TestClient against lineage.server.app (no running server needed; the lifespan
warm-up is not triggered because the client is not used as a context manager). They need the loaded graph,
like the rest of the suite. The static demo tests only read files in the repo.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lineage import server
from lineage.db import connect, graph, resolve

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "docs" / "demo"
WEB = ROOT / "lineage" / "web"
ACTIONS = {"reply_with_ruling", "point_to_existing", "link_prior_threads", "flag_for_review", "route_to_experts"}
LOCAL_ONLY = {"What did I ask earlier?"}   # index.html drops it from MORE in static mode (needs session memory)


# ---------------------------------------------------------------- helpers mirroring scripts/build_static_demo.py

def norm(q: str) -> str:
    """Same as norm() in scripts/build_static_demo.py and lineage/web/index.html."""
    return re.sub(r"[?!.]+$", "", re.sub(r"\s+", " ", q.strip().lower()))


def build_script():
    """scripts/build_static_demo.py as a module, without running main() (it imports lineage, not the graph)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_static_demo", ROOT / "scripts" / "build_static_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def js_const(page: str, name: str) -> str:
    m = re.search(rf"const {name} = (\[.*?\]);\n", page, re.S)
    assert m, f"const {name} not found in index.html"
    return re.sub(r",\s*\]", "]", m.group(1))   # JS allows a trailing comma, JSON does not


def page_questions(page: str) -> list[str]:
    """Every question the home page offers as a button: TRIES (first of each pair), MORE and HERO_Q."""
    tries = [pair[0] for pair in json.loads(js_const(page, "TRIES"))]
    more = json.loads(js_const(page, "MORE"))
    hero = re.search(r'const HERO_Q = "([^"]+)";', page).group(1)
    assert tries and more
    return tries + [m for m in more if m not in LOCAL_ONLY] + [hero]


def check_triage_row(r: dict):
    assert set(r) == {"topic", "title", "url", "at", "action", "reason", "evidence_peps", "routed_to",
                      "evidence", "routed", "draft"}, r
    assert isinstance(r["topic"], int) and not isinstance(r["topic"], bool)
    for k in ("title", "url", "at", "reason"):
        assert isinstance(r[k], str) and r[k], (k, r)
    assert r["action"] in ACTIONS, r["action"]
    assert isinstance(r["evidence_peps"], list) and all(isinstance(n, int) for n in r["evidence_peps"]), r
    assert isinstance(r["routed_to"], list) and all(isinstance(p, str) for p in r["routed_to"]), r
    for e in r["evidence"]:
        assert set(e) == {"kind", "id", "title", "url"} and e["kind"] in ("pep", "thread"), e
        assert isinstance(e["id"], int) and isinstance(e["title"], str) and isinstance(e["url"], str), e
    assert [e["id"] for e in r["evidence"] if e["kind"] == "pep"] == r["evidence_peps"], r
    assert all(set(x) == {"name", "why"} for x in r["routed"]) and [x["name"] for x in r["routed"]] == r["routed_to"], r
    assert r["draft"] is None or isinstance(r["draft"], str), r


# ---------------------------------------------------------------- API (needs the graph)

@pytest.fixture(scope="module")
def client():
    if resolve("python") not in connect().list_graphs():
        pytest.skip("graph not loaded")
    yield TestClient(server.app)
    # /api/ask writes session memory into the graph: remove what these tests asked
    graph().query("MATCH (s:Session {id: 'smoketest'}) OPTIONAL MATCH (s)-[:ASKED]->(q) DETACH DELETE q, s")


@pytest.fixture(autouse=True)
def no_rate_limit():
    server._hits.clear()
    yield
    server._hits.clear()


def test_index_serves_the_page_with_triage_link(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert 'href="#triage"' in r.text and "LINEAGE_STATIC =" not in r.text   # live page, not the snapshot


def test_hero_is_a_recorded_answer(client):
    r = client.get("/hero.json")
    assert r.status_code == 200
    h = r.json()
    assert h["steps"] and h["answer"] and norm(h["question"]) == norm(build_script().HERO)
    assert any(s["outcome"] == "answered" for s in h["steps"])


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    h = r.json()
    assert h["ok"] is True and h["live_graph"] in h["graphs"]


def test_triage_rows_have_the_documented_shape(client):
    r = client.get("/api/triage", params={"limit": 5})
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert 0 < len(rows) <= 5
    for row in rows:
        check_triage_row(row)
    ats = [row["at"] for row in rows]
    assert ats == sorted(ats, reverse=True), "newest first"


@pytest.mark.parametrize("limit", [0, 101, -1, "x"])
def test_triage_limit_validation(client, limit):
    assert client.get("/api/triage", params={"limit": limit}).status_code == 422


def test_ask_answers_with_paths_and_cypher(client):
    r = client.post("/api/ask", json={"question": "Has anyone proposed a do-while loop?", "session": "smoketest"})
    assert r.status_code == 200, r.text
    res = r.json()
    main = [s for s in res["steps"] if s["outcome"] == "answered" and s["tool"] not in ("resolve", "recall", "session_link")]
    assert main, res["steps"]
    assert main[-1]["paths"] and main[-1]["cypher"] and all(c.strip() for c in main[-1]["cypher"])
    assert "315" in res["answer"]   # PEP 315 rejected do-while


@pytest.mark.parametrize("body", [{"question": "x" * 501}, {"question": "a"}, {"question": "ok?", "session": "bad id!"},
                                  {"question": "ok?", "tenant": "x" * 41}])
def test_ask_rejects_bad_input(client, body):
    assert client.post("/api/ask", json=body).status_code == 422


def test_ask_rejects_bad_tenant_before_answering(client):
    assert client.post("/api/ask", json={"question": "ok?", "tenant": "Py-thon"}).status_code == 400


# ---------------------------------------------------------------- static demo (docs/demo, files only)

@pytest.fixture(scope="module")
def answers():
    return json.loads((DEMO / "answers.json").read_text())


@pytest.fixture(scope="module")
def demo_page():
    return (DEMO / "index.html").read_text()


def test_demo_page_is_the_live_page_plus_the_static_flag(demo_page):
    flag = re.search(r'<script>window\.LINEAGE_STATIC = \{date: "\d{4}-\d{2}-\d{2}", count: (\d+)\};</script>\n', demo_page)
    assert flag, "docs/demo/index.html must set window.LINEAGE_STATIC"
    assert demo_page.replace(flag.group(0), "", 1) == (WEB / "index.html").read_text(), \
        "docs/demo/index.html is stale: rerun scripts/build_static_demo.py"


def test_demo_count_matches_answers(demo_page, answers):
    count = int(re.search(r"count: (\d+)", demo_page).group(1))
    assert count == len(answers)


def test_answers_keys_are_normalized_questions(answers):
    assert answers
    for key, res in answers.items():
        assert key == norm(key), key
        assert norm(res["question"]) == key, (key, res["question"])
        assert "session" not in res and isinstance(res["steps"], list) and isinstance(res["answer"], str)


def test_every_offered_question_is_recorded(demo_page, answers):
    mod = build_script()
    offered = page_questions(demo_page) + mod.SEEDS
    assert mod.HERO in offered and mod.HERO == re.search(r'const HERO_Q = "([^"]+)";', demo_page).group(1)
    missing = [q for q in offered if norm(q) not in answers]
    assert not missing, missing


def test_demo_hero_matches_recorded_answer(answers):
    hero = json.loads((DEMO / "hero.json").read_text())
    assert hero == answers[norm(build_script().HERO)]
    assert hero == json.loads((WEB / "hero.json").read_text()), "lineage/web/hero.json and docs/demo/hero.json differ"


def test_follow_ups_of_seed_answers_are_recorded(answers):
    """The crawl records follow-ups to depth 2, so every seed answer's follow-up buttons must resolve."""
    mod = build_script()
    missing = [(q, f) for q in mod.SEEDS for f in mod.follow_ups(answers[norm(q)]) if norm(f) not in answers]
    assert not missing, missing


EVIDENCE_HOSTS = ("https://peps.python.org/", "https://discuss.python.org/")


def test_demo_triage_rows_carry_their_evidence():
    """The triage cards render the agent's own evidence, routing and draft from docs/demo/triage.json."""
    rows = json.loads((DEMO / "triage.json").read_text())["rows"]
    assert rows
    for r in rows:
        check_triage_row(r)
        assert isinstance(r["evidence"], list) and isinstance(r["routed"], list), r["topic"]
        for e in r["evidence"]:
            assert e["kind"] in ("pep", "thread") and isinstance(e["id"], int) and not isinstance(e["id"], bool), e
            assert isinstance(e["title"], str) and e["title"].strip(), e
            assert isinstance(e["url"], str) and e["url"].startswith(EVIDENCE_HOSTS), e
            if e["kind"] == "pep":
                assert e["url"] == f"https://peps.python.org/pep-{e['id']:04d}/", e
        for x in r["routed"]:
            assert isinstance(x["name"], str) and x["name"] and (x["why"] is None or isinstance(x["why"], str)), x
        assert r["draft"] is None or (isinstance(r["draft"], str) and r["draft"].strip()), r["topic"]
        if r["action"] == "flag_for_review":
            assert r["evidence"], f"flag_for_review row {r['topic']} has no evidence for a person to judge"


def test_js_and_python_norm_agree(demo_page):
    """norm() in index.html must match norm() in the build script, or the static demo's buttons miss their
    recorded answers. Runs the page's own JS with node."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    nm = re.search(r"const norm = .*?;\n", demo_page).group(0)
    mod = build_script()
    qs = page_questions(demo_page) + list(mod.SEEDS) + ["  Why   Does Python...?! ", "What?? changed.", "x"]
    js = nm + f"\nconsole.log(JSON.stringify({json.dumps(qs)}.map(norm)));"
    out = json.loads(subprocess.run([node, "-e", js], capture_output=True, text=True, check=True, timeout=30).stdout)
    assert out == [norm(q) for q in qs] == [mod.norm(q) for q in qs]


# ---------------------------------------------------------------- docs: relative links resolve (network links: scripts/smoke.py --links)

def slug(heading: str) -> str:
    """GitHub's heading anchor."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


@pytest.mark.parametrize("doc", ["README.md", "docs/SUBMISSION.md"])
def test_relative_links_resolve(doc):
    path = ROOT / doc
    text = path.read_text()
    anchors = {slug(h) for h in re.findall(r"^#+\s+(.+)$", text, re.M)}
    bad = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text) + re.findall(r'(?:src|href)="([^"]+)"', text):
        if re.match(r"[a-z]+:", target):
            continue
        file, _, anchor = target.partition("#")
        if file and not (path.parent / file).exists():
            bad.append(target)
        elif not file and anchor not in anchors:
            bad.append(target)
    assert not bad, bad


def test_docs_quote_current_demo_counts(answers):
    """Any "<n> answers" / "<n> triage decisions" the docs quote for the demo must match docs/demo."""
    triage_n = len(json.loads((DEMO / "triage.json").read_text())["rows"])
    stale = []
    for doc in ("README.md", "docs/SUBMISSION.md"):
        text = (ROOT / doc).read_text()
        stale += [(doc, f"{n} answers") for n in re.findall(r"\b(\d+) (?:recorded )?answers\b", text) if int(n) != len(answers)]
        stale += [(doc, f"{n} triage decisions") for n in re.findall(r"\b(\d+) triage decisions\b", text) if int(n) != triage_n]
    assert not stale, f"docs/demo has {len(answers)} answers and {triage_n} triage decisions: {stale}"
