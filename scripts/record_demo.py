"""Record the README demo with Playwright's record_video (1280x800).

Storyboard (one browser context, one video file per page, stitched later by build_media.py):
  1. terminal   - the CLI command being typed
  2. crawl      - a REAL headed crawl of 3 categories (1 worker page) with a HUD injected by the
                  crawler's on_page hook: every extracted product is outlined with its parsed price/rating
  3. terminal   - the real log of that run, then the real log of the full run
                  (examples/output/run_events.jsonl), fast-forwarded
  4. pdf        - examples/output/report.pdf rendered by pdf.js inside Chromium
  5. xlsx       - examples/output/books.xlsx rendered by scripts/xlsx_preview.py, clicking through sheets
  6. end card   - numbers of the full run

Outputs: media_build/video/*.webm, media_build/timeline.json, media_build/shots/*.png

    python scripts/record_demo.py            (needs examples/output from a full run first)
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import shlex
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import BrowserContext, Page, Route, async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from scraper.cli import build_parser, settings_from_args, summary_lines  # noqa: E402
from scraper.crawler import CrawlSettings, PageEvent  # noqa: E402
from scraper.log import ConsoleFormatter, format_console, setup_logging  # noqa: E402
from scraper.models import Book  # noqa: E402
from scraper.pipeline import build_outputs, scrape  # noqa: E402
from xlsx_preview import render as render_xlsx  # noqa: E402

BUILD = ROOT / "media_build"
VIDEO_DIR = BUILD / "video"
SHOTS = BUILD / "shots"
FULL = ROOT / "examples" / "output"
DEMO_OUT = ROOT / "demo_out"
VIEW = {"width": 1280, "height": 800}
DEMO_CATEGORIES = "Travel,Mystery,Poetry"
# The command typed on screen. It is parsed by the real CLI parser (see main), so the recorded crawl
# runs with exactly these settings (delay included: the default 0.5 s, as the log shows).
DEMO_CMD = f'python -m scraper --categories "{DEMO_CATEGORIES}" --max-pages 2 --concurrency 1 --headed --out demo_out'
PDFJS = "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/"
ORIGIN = "https://demo.local/"

TERMINAL_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
 html,body{margin:0;height:100%;background:#15171c;font:15px/1.5 "Cascadia Mono",Consolas,monospace;color:#d4d4d4}
 .win{position:absolute;inset:26px;border-radius:10px;background:#0f1115;box-shadow:0 10px 40px #0008;
      display:flex;flex-direction:column;overflow:hidden;border:1px solid #2a2e37}
 .bar{height:38px;background:#1c1f26;display:flex;align-items:center;gap:10px;padding:0 14px;
      font:13px "Segoe UI",Arial,sans-serif;color:#aeb4c0}
 .dot{width:12px;height:12px;border-radius:50%;background:#3a3f4b}
 .badge{background:#eb6834;color:#fff;border-radius:10px;padding:1px 9px;font-weight:700;font-size:11px}
 .ff{margin-left:14px;background:#2a78d6;color:#fff;border-radius:10px;padding:2px 10px;font-size:12px;
     font-weight:600;visibility:hidden}
 .ff.on{visibility:visible}
 #out{flex:1;padding:14px 18px;overflow:hidden;white-space:pre}
 .p{color:#7ee787}.c{color:#fff}.i{color:#79c0ff}.w{color:#e3b341}.e{color:#ff7b72}.m{color:#8b949e}
 .ok{color:#7ee787;font-weight:700}.caret{display:inline-block;width:9px;height:17px;background:#d4d4d4;
     vertical-align:-3px;animation:b 1s steps(1) infinite}@keyframes b{50%{opacity:0}}
</style></head><body><div class="win"><div class="bar"><span class="dot"></span><span class="dot"></span>
<span class="dot"></span><span class="badge">DEMO</span><span>Terminal - playwright-scraper-demo</span>
<span class="ff" id="ff"></span></div><div id="out"></div></div>
<script>
const out = document.getElementById('out');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const esc = s => s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function colorize(line) {
  const m = line.match(/^(\\d\\d:\\d\\d:\\d\\dZ) (\\w+)\\s+(\\S+)(\\s+)(.*)$/);
  if (!m) return esc(line);
  const lv = m[2] === 'INFO' ? 'i' : (m[2] === 'WARNING' ? 'w' : 'e');
  return `<span class="m">${m[1]}</span> <span class="${lv}">${m[2].padEnd(5)}</span> ` +
         `<span class="c">${esc(m[3])}</span>${m[4]}${esc(m[5])}`;
}
function scroll() { out.scrollTop = out.scrollHeight; }
function add(htmlLine) { const d = document.createElement('div'); d.innerHTML = htmlLine || '&nbsp;';
  out.appendChild(d); scroll(); }
window.prompt_ = async (cmd, msPerChar) => {
  const d = document.createElement('div'); out.appendChild(d);
  const pre = '<span class="p">PS playwright-scraper-demo&gt;</span> ';
  for (let i = 0; i <= cmd.length; i++) { d.innerHTML = pre + '<span class="c">' + esc(cmd.slice(0, i)) +
    '</span><span class="caret"></span>'; if (msPerChar) await sleep(msPerChar); }
  d.innerHTML = pre + '<span class="c">' + esc(cmd) + '</span>'; scroll();
};
window.lines = async (arr, msPerLine) => { for (const l of arr) { add(colorize(l)); if (msPerLine) await sleep(msPerLine); } };
window.raw = (htmlLines) => htmlLines.forEach(add);
window.ff = (text) => { const f = document.getElementById('ff'); f.textContent = text; f.classList.toggle('on', !!text); };
</script></body></html>"""

HUD_JS = """
(a) => {
  if (!document.getElementById('__demo_css')) {
    const st = document.createElement('style'); st.id = '__demo_css';
    st.textContent = `
      #__hud{position:fixed;right:18px;bottom:18px;z-index:99999;width:318px;background:#0f1115f2;color:#e6e6e6;
        border-radius:12px;padding:14px 16px;font:13px/1.45 "Segoe UI",Arial,sans-serif;box-shadow:0 8px 30px #0007}
      #__hud .t{display:flex;align-items:center;gap:8px;font-weight:700;font-size:13.5px;margin-bottom:8px}
      #__hud .b{background:#eb6834;color:#fff;border-radius:9px;padding:0 8px;font-size:11px}
      #__hud .g{width:9px;height:9px;border-radius:50%;background:#1baf7a;box-shadow:0 0 0 0 #1baf7a;
        animation:__p 1.2s infinite}
      @keyframes __p{70%{box-shadow:0 0 0 7px #1baf7a00}100%{box-shadow:0 0 0 0 #1baf7a00}}
      #__hud .r{display:flex;justify-content:space-between}#__hud .r span:first-child{color:#9aa3b2}
      #__hud .bar{height:6px;background:#2b303b;border-radius:3px;margin-top:9px;overflow:hidden}
      #__hud .bar i{display:block;height:100%;background:#1baf7a;border-radius:3px}
      article.product_pod{position:relative;transition:box-shadow .15s}
      article.product_pod.__hit{box-shadow:0 0 0 3px #1baf7a;border-radius:4px}
      article.product_pod .__tag{position:absolute;left:6px;top:6px;background:#1baf7a;color:#fff;
        font:700 11.5px "Segoe UI",Arial,sans-serif;border-radius:6px;padding:2px 7px;z-index:5}`;
    document.head.appendChild(st);
  }
  let hud = document.getElementById('__hud');
  if (!hud) { hud = document.createElement('div'); hud.id = '__hud'; document.body.appendChild(hud); }
  hud.innerHTML = `<div class="t"><span class="g"></span>Playwright scraper<span class="b">DEMO</span>
    <span style="margin-left:auto;color:#9aa3b2;font-weight:400">headed run</span></div>
    <div class="r"><span>Category</span><span>${a.category} (${a.catIndex}/${a.catCount})</span></div>
    <div class="r"><span>Listing page</span><span>${a.page} of ${a.totalPages}</span></div>
    <div class="r"><span>Extracted on page</span><span>${a.items} books, validated</span></div>
    <div class="r"><span>Collected so far</span><span><b>${a.total} books</b></span></div>
    <div class="bar"><i style="width:${Math.round(100 * a.catIndex / a.catCount)}%"></i></div>`;
  const pods = [...document.querySelectorAll('article.product_pod')];
  return new Promise(res => {
    pods.forEach((p, i) => setTimeout(() => {
      const href = p.querySelector('h3 a').href, info = a.byUrl[href];
      p.classList.add('__hit');
      if (info) { const t = document.createElement('div'); t.className = '__tag';
        t.textContent = '\\u2713 \\u00a3' + info.price.toFixed(2) + ' \\u00b7 ' + info.rating + '\\u2605';
        p.appendChild(t); }
    }, i * a.stagger));
    setTimeout(res, pods.length * a.stagger + 150);
  });
}
"""

SCROLL_JS = """
([y, ms]) => new Promise(res => {
  const el = document.scrollingElement, y0 = el.scrollTop, t0 = performance.now();
  const ease = t => t < .5 ? 2*t*t : 1 - Math.pow(-2*t + 2, 2) / 2;
  function step(now) { const t = Math.min(1, (now - t0) / ms); el.scrollTop = y0 + (y - y0) * ease(t);
    t < 1 ? requestAnimationFrame(step) : res(); }
  requestAnimationFrame(step);
})
"""

CURSOR_JS = """
([x, y, ms]) => new Promise(res => {
  let c = document.getElementById('__cur');
  if (!c) {
    c = document.createElement('div'); c.id = '__cur';
    c.innerHTML = '<svg width="22" height="28" viewBox="0 0 22 28"><path d="M2 2 L2 22 L7.5 17 L11 25.5 L14.5 24 ' +
      'L11 15.8 L18.5 15.8 Z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    c.style.cssText = 'position:fixed;left:0;top:0;z-index:999999;pointer-events:none;transform:translate(640px,520px)';
    document.body.appendChild(c); c.getBoundingClientRect();
  }
  c.style.transition = `transform ${ms}ms cubic-bezier(.4,0,.2,1)`;
  c.style.transform = `translate(${x}px,${y}px)`;
  setTimeout(res, ms + 40);
})
"""

PDF_VIEWER = """<!doctype html><html><head><meta charset="utf-8"><title>report.pdf</title><style>
 html,body{margin:0;background:#3b3e44;font:13px "Segoe UI",Arial,sans-serif}
 .tb{position:fixed;top:0;left:0;right:0;height:46px;background:#23252a;color:#e8e8e8;display:flex;align-items:center;
     gap:16px;padding:0 18px;z-index:5;box-shadow:0 2px 8px #0006}
 .tb b{font-size:14px}.pill{background:#eb6834;color:#fff;border-radius:10px;padding:1px 9px;font-weight:700;font-size:11px}
 .mid{margin:0 auto;display:flex;gap:14px;align-items:center;color:#cfd3da}
 .box{background:#15171b;border-radius:4px;padding:2px 9px;color:#fff}
 .src{color:#9aa3b2}
 #pages{padding:66px 0 30px;display:flex;flex-direction:column;align-items:center;gap:18px}
 canvas{background:#fff;box-shadow:0 4px 18px #0008}
</style></head><body>
<div class="tb"><span class="pill">DEMO</span><b>report.pdf</b><span class="src">examples/output/report.pdf</span>
<div class="mid"><span>Page <span class="box" id="pg">1</span> / <span id="np">-</span></span><span class="box">125%</span></div>
<span class="src">rendered with pdf.js in Chromium</span></div>
<div id="pages"></div>
<script type="module">
import * as pdfjsLib from '/pdfjs/pdf.min.mjs';
pdfjsLib.GlobalWorkerOptions.workerSrc = '/pdfjs/pdf.worker.min.mjs';
const pdf = await pdfjsLib.getDocument('/report.pdf').promise;
document.getElementById('np').textContent = pdf.numPages;
const holder = document.getElementById('pages'), ratio = 2, tops = [];
for (let n = 1; n <= pdf.numPages; n++) {
  const page = await pdf.getPage(n), vp = page.getViewport({ scale: 1.25 });
  const c = document.createElement('canvas');
  c.width = vp.width * ratio; c.height = vp.height * ratio;
  c.style.width = vp.width + 'px'; c.style.height = vp.height + 'px';
  holder.appendChild(c);
  await page.render({ canvasContext: c.getContext('2d'), viewport: vp, transform: [ratio, 0, 0, ratio, 0, 0] }).promise;
}
const canv = [...document.querySelectorAll('canvas')];
window.pageTops = canv.map(c => c.offsetTop - 60);
addEventListener('scroll', () => {
  const y = scrollY + 300; let p = 1; canv.forEach((c, i) => { if (c.offsetTop <= y) p = i + 1; });
  document.getElementById('pg').textContent = p;
});
window.__ready = true;
</script></body></html>"""

END_CARD = """<!doctype html><html><head><meta charset="utf-8"><style>
 html,body{margin:0;height:100%;background:#1F2A44;color:#fff;font:18px "Segoe UI",Arial,sans-serif}
 .w{height:100%;display:flex;flex-direction:column;justify-content:center;padding:0 110px;gap:16px}
 .pill{align-self:flex-start;background:#eb6834;border-radius:14px;padding:3px 14px;font-weight:700;font-size:15px}
 h1{margin:0;font-size:46px}.s{color:#C9D3E6;font-size:21px}
 .k{display:flex;gap:14px;margin-top:14px}.k div{background:#ffffff14;border-radius:10px;padding:14px 18px;min-width:120px}
 .k b{display:block;font-size:30px}.k span{color:#C9D3E6;font-size:14px}
 .f{color:#C9D3E6;font-size:17px;margin-top:10px}
</style></head><body><div class="w"><span class="pill">DEMO</span>
<h1>Playwright scraper &rarr; Excel + PDF</h1>
<div class="s">Full catalog run, real numbers from examples/output/run_log.json</div>
<div class="k">{tiles}</div>
<div class="f">Outputs: books.xlsx &middot; books.csv &middot; report.pdf &middot; run_log.json</div>
<div class="f">github.com/joao-jleite/playwright-scraper-demo</div></div></body></html>"""


class ListHandler(logging.Handler):
    """Keeps the formatted console lines of the demo run so the terminal scene shows the real log."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.setFormatter(ConsoleFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def full_run_lines() -> list[str]:
    """examples/output/run_events.jsonl -> the exact console lines of that run (UTC, like the CLI)."""
    out = []
    for raw in (FULL / "run_events.jsonl").read_text(encoding="utf-8").splitlines():
        ev = json.loads(raw)
        ts = datetime.fromisoformat(ev.pop("ts"))
        level, name = ev.pop("level"), ev.pop("event")
        out.append(format_console(ts, level, name, ev))
    return out


class Recorder:
    def __init__(self, context: BrowserContext) -> None:
        self.context = context
        self.segments: list[dict] = []
        self.opened: list[tuple[Page, float]] = []
        self.closed: dict[int, float] = {}
        context.on("page", self._on_page)

    def _on_page(self, page: Page) -> None:
        if all(p is not page for p, _ in self.opened):
            self.opened.append((page, time.monotonic()))
        page.on("close", lambda p: self.closed.setdefault(id(p), time.monotonic()))

    def lifetime(self, page: Page) -> float | None:
        """Wall-clock seconds the page was open. build_media.py uses it to map wall time to video time
        (Playwright's video clock can run slower than wall time on a busy machine)."""
        closed = self.closed.get(id(page))
        return None if closed is None else closed - self.created_at(page)

    def created_at(self, page: Page) -> float:
        for p, t in self.opened:
            if p is page:
                return t
        now = time.monotonic()  # event not delivered yet: the page was created just now
        self.opened.append((page, now))
        return now

    def add(self, name: str, page: Page, plan: list[tuple[float, float, float, str | None]]) -> None:
        """plan: (t0, t1, speed, badge) in seconds relative to the page's creation (= video start)."""
        self.segments.append({"name": name, "page": page, "plan": plan})


async def main() -> None:
    if not (FULL / "report.pdf").exists():
        raise SystemExit("run the full crawl first: python -m scraper --out examples/output")
    for d in (VIDEO_DIR, SHOTS):
        d.mkdir(parents=True, exist_ok=True)
    for old in VIDEO_DIR.glob("*.webm"):
        old.unlink()
    full_log = json.loads((FULL / "run_log.json").read_text(encoding="utf-8"))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, args=["--window-size=1296,920"])
        # pdf.js is fetched once and served from the same fake origin (module workers must be same-origin)
        api = await pw.request.new_context()
        cdn = {}
        for name in ("pdf.min.mjs", "pdf.worker.min.mjs"):
            for attempt in range(1, 5):  # the CDN connection is sometimes reset: retry with backoff
                try:
                    resp = await api.get(PDFJS + name, timeout=30_000)
                    if resp.ok:
                        cdn[name] = await resp.body()
                        break
                    err = f"HTTP {resp.status}"
                except Exception as exc:
                    err = str(exc).splitlines()[0]
                if attempt == 4:
                    raise SystemExit(f"could not fetch {PDFJS + name}: {err}")
                await asyncio.sleep(2 ** attempt)
        await api.dispose()
        (BUILD / "books_preview.html").write_text(render_xlsx(FULL / "books.xlsx"), encoding="utf-8")

        context = await browser.new_context(viewport=VIEW, record_video_dir=str(VIDEO_DIR), record_video_size=VIEW,
                                            locale="en-GB", user_agent=CrawlSettings().user_agent)

        async def serve(route: Route) -> None:
            path = route.request.url.replace(ORIGIN, "").split("?")[0]
            if path.startswith("pdfjs/"):
                await route.fulfill(body=cdn[path[6:]], content_type="text/javascript")
            elif path == "report.pdf":
                await route.fulfill(body=(FULL / "report.pdf").read_bytes(), content_type="application/pdf")
            elif path == "viewer.html":
                await route.fulfill(body=PDF_VIEWER, content_type="text/html; charset=utf-8")
            elif path == "books.xlsx.html":
                await route.fulfill(path=str(BUILD / "books_preview.html"), content_type="text/html; charset=utf-8")
            else:
                await route.fulfill(status=404, body="not found")

        await context.route(ORIGIN + "**", serve)
        rec = Recorder(context)

        # ---------------------------------------------------------------- 1. terminal intro
        term = await context.new_page()
        await term.set_content(TERMINAL_HTML)
        t = lambda: time.monotonic() - rec.created_at(term)  # noqa: E731
        t_start = t()
        await term.wait_for_timeout(280)
        await term.evaluate("([c, ms]) => prompt_(c, ms)", [DEMO_CMD, 13])
        await term.wait_for_timeout(440)
        rec.add("intro", term, [(t_start, t(), 1.0, None)])
        await term.close()

        # ---------------------------------------------------------------- 2. real headed crawl
        DEMO_OUT.mkdir(exist_ok=True)
        log = setup_logging(DEMO_OUT / "run_events.jsonl")
        capture = ListHandler()
        log.addHandler(capture)
        # Exactly the command shown in the terminal scene, parsed by the CLI's own parser.
        settings = settings_from_args(build_parser().parse_args(shlex.split(DEMO_CMD)[3:]))
        settings.out_dir = ROOT / settings.out_dir
        cat_names = DEMO_CATEGORIES.split(",")
        marks: list[tuple[float, float]] = []
        total = {"n": 0}
        crawl_page: list[Page] = []

        async def hud(page: Page, ev: PageEvent, books: list[Book]) -> None:
            t0 = time.monotonic()
            if not crawl_page:
                crawl_page.append(page)
            total["n"] += len(books)
            await page.evaluate(HUD_JS, {
                "category": ev.category, "catIndex": cat_names.index(ev.category) + 1, "catCount": len(cat_names),
                "page": ev.page_no, "totalPages": min(ev.total_pages or 1, 2), "items": len(books),
                "total": total["n"], "stagger": 28,
                "byUrl": {str(b.url): {"price": b.price_gbp, "rating": b.rating} for b in books}})
            await page.wait_for_timeout(350)
            if ev.category == "Mystery" and ev.page_no == 1:
                await page.screenshot(path=str(SHOTS / "01-headed-crawl.png"))
            if ev.page_no == 1 and ev.category in cat_names[:2]:
                # scroll through the first two listings only; later pages just flash their highlights
                # (keeps the GIF under 5 MB: every scrolled frame with book covers costs ~120 KB)
                height = await page.evaluate("document.scrollingElement.scrollHeight - innerHeight")
                await page.evaluate(SCROLL_JS, [height, 800])
                await page.wait_for_timeout(250)
            else:
                await page.wait_for_timeout(450)
            marks.append((t0, time.monotonic()))

        wall_t0 = time.perf_counter()
        outcome = await scrape(context, settings, log, on_page=hud)
        demo_log = build_outputs(outcome, settings, log, wall_t0)
        log.removeHandler(capture)

        # The worker page is the one the hook ran on (the category-discovery page is not shown).
        worker = crawl_page[0]
        worker_t = rec.created_at(worker)
        plan, prev = [], 0.25
        for hs, he in marks:
            plan.append((prev, hs - worker_t, 4.0, "4x"))  # navigation + polite delay: fast-forward
            plan.append((hs - worker_t, he - worker_t, 2.0, "2x"))  # processing + HUD
            prev = he - worker_t
        plan.append((prev, prev + 0.4, 4.0, "4x"))
        rec.add("crawl", worker, plan)

        # ---------------------------------------------------------------- 3. terminal: real logs
        term = await context.new_page()
        await term.set_content(TERMINAL_HTML)
        t = lambda: time.monotonic() - rec.created_at(term)  # noqa: E731
        await term.evaluate("([c]) => prompt_(c, 0)", [DEMO_CMD])
        await term.evaluate("([l]) => lines(l, 0)", [capture.lines + summary_lines(demo_log, "demo_out", "\\")])
        t_a = t()
        await term.wait_for_timeout(750)
        t_b = t()
        await term.evaluate("() => raw([''])")
        await term.evaluate("([c, ms]) => prompt_(c, ms)", ["python -m scraper --out examples/output", 16])
        ff_label = f"▶▶ full run: real log replayed fast ({full_log['crawl_duration_s']:.0f} s run)"
        await term.evaluate("([s]) => ff(s)", [ff_label])
        await term.evaluate("([l, ms]) => lines(l, ms)", [full_run_lines(), 7])
        await term.evaluate("() => ff('')")
        await term.evaluate("([l]) => lines(l, 0)", [summary_lines(full_log, r"examples\output", "\\")])
        t_c = t()
        await term.wait_for_timeout(1100)
        rec.add("log", term, [(t_a - 0.1, t_b, 1.0, None), (t_b, t_c, 3.0, "3x"), (t_c, t(), 1.0, None)])
        await term.close()

        # ---------------------------------------------------------------- 4. PDF in Chromium (pdf.js)
        pdf = await context.new_page()
        await pdf.goto(ORIGIN + "viewer.html")
        await pdf.wait_for_function("window.__ready === true", timeout=30_000)
        t = lambda: time.monotonic() - rec.created_at(pdf)  # noqa: E731
        t_a = t()
        # Page-by-page jumps (like PageDown) instead of smooth scrolling: reads better and keeps the GIF small.
        await pdf.wait_for_timeout(850)
        tops = await pdf.evaluate("window.pageTops")
        for top, hold in ((tops[0] + 330, 600), (tops[1], 1100), (tops[2], 900)):
            await pdf.evaluate(SCROLL_JS, [top, 80])
            await pdf.wait_for_timeout(hold)
        rec.add("pdf", pdf, [(t_a, t(), 1.0, None)])
        await pdf.close()

        # ---------------------------------------------------------------- 5. spreadsheet
        xl = await context.new_page()
        await xl.goto(ORIGIN + "books.xlsx.html")
        t = lambda: time.monotonic() - rec.created_at(xl)  # noqa: E731
        t_a = t()
        await xl.evaluate(CURSOR_JS, [560, 400, 10])
        await xl.wait_for_timeout(1300)  # the workbook opens on Summary (its active sheet)
        for name, hold in (("Data", 700), ("Opportunities", 950)):
            box = await xl.locator(f'.tab[data-sheet="{name}"]').bounding_box()
            await xl.evaluate(CURSOR_JS, [box["x"] + box["width"] / 2 - 4, box["y"] + box["height"] / 2 - 4, 370])
            await xl.click(f'.tab[data-sheet="{name}"]')
            await xl.wait_for_timeout(hold)
        rec.add("xlsx", xl, [(t_a, t(), 1.0, None)])
        await xl.close()

        # ---------------------------------------------------------------- 6. end card
        end = await context.new_page()
        rec_ = full_log["records"]
        tiles = "".join(f"<div><b>{v}</b><span>{k}</span></div>" for k, v in [
            ("products", f"{rec_['exported']:,}"), ("categories", full_log["categories"]["crawled"]),
            ("pages", full_log["pages"]["ok"]), ("errors", full_log["pages"]["failed"] + rec_["invalid"]),
            ("seconds", f"{full_log['crawl_duration_s']:.1f}")])
        await end.set_content(END_CARD.replace("{tiles}", tiles))
        t = lambda: time.monotonic() - rec.created_at(end)  # noqa: E731
        t_a = t()
        await end.wait_for_timeout(1600)
        rec.add("end", end, [(t_a, t(), 1.0, None)])
        await end.close()

        # ---------------------------------------------------------------- still screenshots (sharp, 1.5x)
        shots_ctx = await browser.new_context(viewport=VIEW, device_scale_factor=1.5)
        await shots_ctx.route(ORIGIN + "**", serve)
        sp = await shots_ctx.new_page()
        await sp.goto(ORIGIN + "viewer.html")
        await sp.wait_for_function("window.__ready === true", timeout=30_000)
        tops = await sp.evaluate("window.pageTops")
        await sp.evaluate("y => window.scrollTo(0, y)", tops[1])
        await sp.wait_for_timeout(300)
        await sp.screenshot(path=str(SHOTS / "02-pdf-report.png"))
        await sp.goto(ORIGIN + "books.xlsx.html")
        await sp.evaluate("showSheet('Summary')")
        await sp.wait_for_timeout(200)
        await sp.screenshot(path=str(SHOTS / "03-xlsx-preview.png"))
        await shots_ctx.close()

        await context.close()  # flushes the videos
        timeline = []
        for seg in rec.segments:
            timeline.append({"name": seg["name"], "video": str(Path(await seg["page"].video.path()).relative_to(ROOT)),
                             "lifetime": rec.lifetime(seg["page"]), "plan": seg["plan"]})
        await browser.close()

    (BUILD / "timeline.json").write_text(json.dumps(timeline, indent=2), encoding="utf-8")
    print(json.dumps({"demo_run": {k: demo_log[k] for k in ("status", "crawl_duration_s", "pages", "records")},
                      "segments": [s["name"] for s in timeline]}, indent=2))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
