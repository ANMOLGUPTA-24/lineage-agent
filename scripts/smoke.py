"""Smoke-check a running Lineage instance (and, with --links, every external link in the docs). Stdlib only.

  uv run python scripts/smoke.py                       # http://localhost:8000
  uv run python scripts/smoke.py https://lineage.example.org --links

Prints a PASS/FAIL table; exits 1 if anything failed.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ["README.md", "docs/SUBMISSION.md"]
SKIP_HOSTS = ("medium.com",)                         # blocks bots with 403
LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0")  # run-it-yourself placeholders in the docs
ACTIONS = {"reply_with_ruling", "point_to_existing", "link_prior_threads", "flag_for_review", "route_to_experts"}
SPA_HOSTS = ("anmolgupta-24.github.io",)             # the demo routes #fragments in JS, not by element id
UA = "Mozilla/5.0 (X11; Linux x86_64) lineage-smoke/1.0"


def fetch(url, method="GET", body=None, timeout=30):
    """(status, headers, body bytes). HTTP errors are returned, not raised; network errors raise."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": UA, "Accept": "*/*", **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read() if method != "HEAD" else b""
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read() if method != "HEAD" else b""


def get_json(url, **kw):
    status, _, raw = fetch(url, **kw)
    if status != 200:
        raise AssertionError(f"HTTP {status}: {raw[:200].decode(errors='replace')}")
    return json.loads(raw)


# ---------------------------------------------------------------- API checks: each returns a short detail or raises

def check_index(base):
    status, headers, raw = fetch(base + "/")
    assert status == 200, f"HTTP {status}"
    assert headers.get("Content-Type", "").startswith("text/html"), headers.get("Content-Type")
    assert b'href="#triage"' in raw, "no triage link"
    return f"{len(raw) // 1024} KB html"


def check_hero(base):
    h = get_json(base + "/hero.json")
    assert h.get("steps") and h.get("answer"), "no steps/answer"
    assert any(s.get("outcome") == "answered" for s in h["steps"]), "no answered step"
    return h.get("question", "")[:50]


def check_health(base):
    h = get_json(base + "/api/health")
    assert h.get("ok") is True, h
    assert h.get("live_graph") in h.get("graphs", []), f"live graph {h.get('live_graph')} not in {h.get('graphs')}"
    return f"graph {h['live_graph']}, last sync {(h.get('last_sync') or {}).get('at')}"


def check_triage(base):
    rows = get_json(base + "/api/triage?limit=20")["rows"]
    assert rows, "no triage rows"
    keys = {"topic", "title", "url", "at", "action", "reason", "evidence_peps", "routed_to", "evidence", "routed", "draft"}
    for r in rows:
        assert set(r) == keys, f"keys {sorted(r)}"
        assert isinstance(r["topic"], int) and r["action"] in ACTIONS, (r["topic"], r["action"])
        assert all(isinstance(n, int) for n in r["evidence_peps"]) and all(isinstance(p, str) for p in r["routed_to"])
        assert all(e.get("kind") in ("pep", "thread") and isinstance(e.get("id"), int)
                   and str(e.get("url", "")).startswith(("https://peps.python.org/", "https://discuss.python.org/"))
                   for e in r["evidence"]), f"bad evidence in topic {r['topic']}"
        assert all(isinstance(x.get("name"), str) for x in r["routed"]), f"bad routed in topic {r['topic']}"
        assert r["draft"] is None or isinstance(r["draft"], str), f"bad draft in topic {r['topic']}"
        assert r["action"] != "flag_for_review" or r["evidence"], f"flag_for_review topic {r['topic']} has no evidence"
    status, _, _ = fetch(base + "/api/triage?limit=101")
    assert status == 422, f"limit=101 gave HTTP {status}, want 422"
    return f"{len(rows)} rows"


def check_ask(base):
    t = time.monotonic()
    res = get_json(base + "/api/ask", method="POST", timeout=120,
                   body={"question": "Has anyone proposed a do-while loop?", "session": "smoke"})
    main = [s for s in res.get("steps", []) if s.get("outcome") == "answered"
            and s.get("tool") not in ("resolve", "recall", "session_link")]
    assert main, f"no answered step: {[(s.get('tool'), s.get('outcome')) for s in res.get('steps', [])]}"
    assert main[-1].get("paths") and main[-1].get("cypher"), "answered step has no paths/cypher"
    assert "315" in res.get("answer", ""), "answer does not cite PEP 315"
    return f"{main[-1]['tool']}, {len(main[-1]['paths'])} paths, {time.monotonic() - t:.1f}s"


# ---------------------------------------------------------------- link checks

def doc_links():
    """{url: [doc:line, ...]} for every http(s) link in the docs."""
    out = {}
    for doc in DOCS:
        for n, line in enumerate((ROOT / doc).read_text().splitlines(), 1):
            for url in re.findall(r"https?://[^\s)\"'<>`\]]+", line):
                url = url.rstrip(".,;:*")
                out.setdefault(url, []).append(f"{doc}:{n}")
    return out


def check_link(url):
    host = urllib.request.urlparse(url).hostname or ""
    if any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
        return "SKIP", "host blocks bots"
    if host in LOCAL_HOSTS:
        return "SKIP", "local address"
    frag = urllib.request.urlparse(url).fragment
    check_frag = bool(frag) and host not in SPA_HOSTS
    last = ""
    for attempt in range(3):   # a retry for transient network errors and 429/5xx
        try:
            status, headers, raw = fetch(url, method="GET" if check_frag else "HEAD", timeout=20)
            if status in (403, 405, 501) or status >= 500:   # some hosts refuse HEAD: fall back to a GET
                status, headers, raw = fetch(url, timeout=20)
        except Exception as e:   # DNS, TLS, timeout
            last = f"{type(e).__name__}: {e}"
        else:
            if 200 <= status < 400:
                ctype = headers.get("Content-Type", "")
                if url.endswith(".mp4") and not ctype.startswith("video/mp4"):
                    return "FAIL", f"HTTP {status}, Content-Type {ctype!r}, want video/mp4"
                if check_frag and not re.search(rf'(?:id|name)="{re.escape(frag)}"'.encode(), raw):
                    return "FAIL", f"HTTP {status}, but no element with id #{frag}"
                return "PASS", f"HTTP {status}" + (f", {ctype}" if url.endswith(".mp4") else "")
            last = f"HTTP {status}"
            if status != 429 and status < 500:
                break
        time.sleep(1 + 2 * attempt)
    return "FAIL", last


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("base", nargs="?", default="http://localhost:8000", help="base URL (default %(default)s)")
    ap.add_argument("--links", action="store_true", help="also check every http(s) link in README.md and docs/SUBMISSION.md")
    ap.add_argument("--no-api", action="store_true", help="skip the API checks (e.g. links only)")
    a = ap.parse_args(argv)
    base = a.base.rstrip("/")
    results = []   # (status, name, detail)
    if not a.no_api:
        for name, fn in [("GET /", check_index), ("GET /hero.json", check_hero), ("GET /api/health", check_health),
                         ("GET /api/triage", check_triage), ("POST /api/ask", check_ask)]:
            try:
                results.append(("PASS", name, fn(base)))
            except Exception as e:
                results.append(("FAIL", name, f"{type(e).__name__}: {e}"[:160]))
    if a.links:
        links = doc_links()
        with ThreadPoolExecutor(8) as pool:
            for url, (status, detail) in zip(links, pool.map(check_link, links)):
                results.append((status, url if len(url) <= 60 else url[:57] + "...", f"{detail} ({links[url][0]})"))
    width = min(60, max(len(n) for _, n, _ in results))
    for status, name, detail in results:
        print(f"{status:4}  {name:<{width}}  {detail}")
    failed = sum(s == "FAIL" for s, _, _ in results)
    print(f"\n{sum(s == 'PASS' for s, _, _ in results)} passed, {failed} failed, "
          f"{sum(s == 'SKIP' for s, _, _ in results)} skipped  ({base})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
