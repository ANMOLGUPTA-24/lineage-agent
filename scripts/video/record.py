"""Record one browser clip per scene of the demo video, timed to that scene's narration.

  python scripts/video/record.py <voice-dir> <out-dir>

<voice-dir> holds vo_<id>.wav and vo_<id>.json (per-sentence durations) from the TTS step.
Needs the web server on http://localhost:8000 and Playwright (Chromium). Writes <out-dir>/<id>.webm and
<out-dir>/clips.json: for each scene, the clip, where the scene starts inside it, and its length.
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
APP = "http://localhost:8000/"
SLIDES = (HERE / "slides.html").resolve().as_uri()
W, H = 1600, 900
LEAD, TAIL = 0.5, 0.9   # silence before and after each scene's narration

# A visible cursor: headless recordings don't draw the system one.
CURSOR = """
(() => { if (document.getElementById('fake-cursor')) return;
  const c = document.createElement('div'); c.id = 'fake-cursor';
  c.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24"><path d="M3 2l7 19 2.6-7.4L20 11z" fill="#fff" stroke="#111" stroke-width="1.5"/></svg>';
  Object.assign(c.style, {position: 'fixed', left: '-40px', top: '-40px', zIndex: 99999, pointerEvents: 'none', transition: 'left .05s, top .05s'});
  document.documentElement.appendChild(c);
  addEventListener('mousemove', (e) => { c.style.left = e.clientX - 3 + 'px'; c.style.top = e.clientY - 2 + 'px'; }, true);
})();"""


class Scene:
    def __init__(self, browser, out: Path, sid: str, voice: Path):
        self.sid, self.out = sid, out
        self.chunks = json.load(open(voice / f"vo_{sid}.json"))
        self.dur = LEAD + sum(d for _, d in self.chunks) + TAIL
        self.ctx = browser.new_context(viewport={"width": W, "height": H}, color_scheme="dark",
                                       record_video_dir=str(out / sid), record_video_size={"width": W, "height": H})
        self.ctx.add_init_script(CURSOR)
        self.page = self.ctx.new_page()
        self.born = time.monotonic()
        self.t0 = None

    def start(self):
        """The scene starts now: everything before this is setup and is cut."""
        self.t0 = time.monotonic()

    def at(self, k: int) -> float:
        """Seconds into the scene where sentence k starts."""
        return LEAD + sum(d for _, d in self.chunks[:k])

    def wait_until(self, s: float):
        left = self.t0 + s - time.monotonic()
        if left > 0:
            self.page.wait_for_timeout(left * 1000)

    def glide(self, x: float, y: float, steps: int = 25):
        self.page.mouse.move(x, y, steps=steps)

    def finish(self) -> dict:
        self.wait_until(self.dur + 0.2)
        offset = self.t0 - self.born
        self.page.close()
        video = self.page.video.path()
        self.ctx.close()
        return {"id": self.sid, "video": video, "offset": round(offset, 3), "dur": round(self.dur, 3)}


def home(sc: Scene):
    sc.page.goto(APP + "?v=video")
    sc.page.wait_for_selector("#cy-feature canvas")


def ask(sc: Scene, question: str):
    sc.glide(640, 365)
    sc.page.click("#q")
    sc.page.keyboard.type(question, delay=38)
    sc.page.wait_for_timeout(250)
    sc.page.keyboard.press("Enter")
    sc.page.wait_for_selector(".answer .cy canvas", timeout=20000)


def smooth_scroll(sc: Scene, y: int, ms: int = 1200):
    sc.page.evaluate(f"window.scrollTo({{top: {y}, behavior: 'smooth'}})")
    sc.page.wait_for_timeout(ms)


def scroll_to(sc: Scene, selector: str, offset: int = 110):
    y = sc.page.evaluate(f"(() => {{ const e = document.querySelector({json.dumps(selector)}); "
                         f"return e ? e.getBoundingClientRect().top + scrollY - {offset} : 0; }})()")
    smooth_scroll(sc, int(y))


def scene_hook(sc):
    sc.page.goto(APP + "?v=video")
    sc.start()                                  # include the opening animation
    sc.page.wait_for_selector("#cy-feature canvas")
    sc.wait_until(sc.at(1))
    sc.glide(300, 365, 40)                      # toward the question box
    sc.wait_until(sc.at(2))
    sc.glide(330, 445, 30)


def scene_what(sc):
    home(sc)
    sc.page.wait_for_timeout(2500)
    sc.start()
    for k, y in enumerate([443, 510, 590, 660]):    # the example questions
        sc.glide(260, y, 18)
        sc.page.wait_for_timeout(700)
    sc.wait_until(sc.at(1))
    sc.glide(1110, 420, 40)                     # onto the graph: PEP 505
    sc.wait_until(sc.at(2))
    sc.glide(1180, 470, 30)


def scene_hero(sc):
    home(sc)
    sc.page.wait_for_timeout(2500)
    sc.start()
    sc.glide(1100, 440, 20)
    sc.wait_until(sc.at(1))
    scroll_to(sc, ".feature .history", 330)
    sc.glide(1000, 600, 30)
    sc.wait_until(sc.at(3))
    sc.glide(1100, 380, 30)


def scene_switch(sc):
    home(sc)
    sc.page.wait_for_timeout(1500)
    sc.start()
    ask(sc, "Should Python add a switch statement?")
    sc.page.evaluate("window.scrollTo(0, 0)")
    sc.wait_until(sc.at(1))
    sc.glide(420, 470, 30)                      # the quoted rejection notice
    sc.wait_until(sc.at(2))
    sc.glide(1180, 520, 30)                     # the graph
    sc.wait_until(sc.at(3))
    scroll_to(sc, ".answer .record:nth-of-type(2)", 160)


def scene_chain(sc):
    home(sc)
    sc.page.wait_for_timeout(1500)
    sc.start()
    ask(sc, "Draft a reply to someone proposing keyword arguments in indexing")
    sc.page.evaluate("window.scrollTo(0, 0)")
    sc.wait_until(sc.at(2))
    scroll_to(sc, ".answer .history", 260)
    sc.wait_until(sc.at(3))
    scroll_to(sc, ".answer .record", 120)
    sc.page.click("text=Show full reply")
    sc.glide(380, 400, 30)


def scene_explain(sc):
    home(sc)
    ask(sc, "Has anyone proposed keyword arguments in indexing?")
    sc.page.evaluate("window.scrollTo(0, 0)")
    sc.page.wait_for_timeout(1200)
    sc.start()
    sc.glide(1320, 285, 30)
    sc.page.click("[data-expand]")              # the full-window graph
    sc.page.wait_for_timeout(2600)
    sc.page.keyboard.press("Escape")
    if sc.page.is_visible("#ov-close"):
        sc.page.click("#ov-close")
    sc.wait_until(sc.at(1) - 1.2)
    scroll_to(sc, ".how-sum", 140)
    sc.page.click(".how-sum > summary")
    sc.page.wait_for_timeout(500)
    sc.page.evaluate("document.querySelectorAll('.how-sum details').forEach((d) => d.open = true)")
    scroll_to(sc, ".how-sum details pre", 330)


def scene_triage(sc):
    # Show the live run only: a backfill over forum history can push the showcase topic past the page's 100 rows.
    sc.page.route("**/api/triage?*", lambda route: route.continue_(url=APP + "api/triage?limit=100&mode=live"))
    home(sc)
    sc.page.wait_for_timeout(1200)
    sc.start()
    sc.glide(1180, 30, 25)                      # the header link
    sc.page.click("#nav-triage")
    sc.page.wait_for_selector(".t-sum")
    sc.glide(520, 300, 25)                      # the summary line
    sc.wait_until(sc.at(1) - 0.6)
    sc.page.click("[data-act=decided]")         # replies drafted: the showcase card is among them
    card = ".t-row:has(a[href$='/t/108762'])"
    scroll_to(sc, card, 90)
    sc.glide(420, 230, 25)                      # its stamp and reason
    sc.wait_until(sc.at(2))
    sc.page.click(f"{card} .t-how > summary")   # how it decided
    sc.page.wait_for_timeout(300)
    scroll_to(sc, f"{card} .t-how", 120)
    sc.glide(300, 400, 30)
    sc.wait_until(sc.at(3))
    scroll_to(sc, f"{card} .fields", 140)       # back up to the fields
    sc.glide(330, 330, 25)                      # wrote to the graph, sent to
    sc.wait_until(sc.at(4))
    sc.page.click(f"{card} .t-draft > summary")
    sc.page.wait_for_timeout(300)
    scroll_to(sc, f"{card} .t-draft", 50)
    sc.glide(400, 420, 30)


def slide(name, steps):
    """steps[k]: the slide element revealed when sentence k starts."""
    def run(sc):
        sc.page.goto(f"{SLIDES}?s={name}")
        sc.page.wait_for_timeout(1200)          # fonts
        sc.start()
        for k, step in enumerate(steps[:len(sc.chunks)]):
            sc.wait_until(sc.at(k) + 0.15)
            sc.page.evaluate(f"reveal({step})")
    return run


SCENES = {"hook": scene_hook, "what": scene_what, "hero": scene_hero, "switch": scene_switch, "chain": scene_chain,
          "explain": scene_explain, "triage": scene_triage, "how": slide("how", [0, 1, 2, 3, 4]),
          "trust": slide("results", [0, 0, 1, 1, 1, 1, 2]), "close": slide("end", [0, 0, 1, 1])}


def main():
    voice, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    ids = [s["id"] for s in json.load(open(HERE / "script.json"))]
    only = set(sys.argv[3:]) or set(ids)
    clips = json.load(open(out / "clips.json")) if (out / "clips.json").exists() else {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for sid in ids:
            if sid not in only:
                continue
            sc = Scene(browser, out, sid, voice)
            SCENES[sid](sc)
            clips[sid] = sc.finish()
            print(clips[sid])
        browser.close()
    json.dump(clips, open(out / "clips.json", "w"), indent=1)


if __name__ == "__main__":
    main()
