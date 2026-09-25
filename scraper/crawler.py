"""Async Playwright crawler: categories -> paginated listings -> validated `Book` rows.

Design notes
- A fixed pool of N worker pages pulls tasks from an asyncio.Queue, so N is the hard
  concurrency cap (default 4 open pages).
- Pagination is discovered on the fly: a listing page enqueues its "next" page, which
  lets different categories progress in parallel while each category stays ordered.
- Every navigation goes through `_goto_with_retry` (exponential backoff + jitter);
  after every page the worker sleeps `delay_s` (+ jitter) to stay polite.
- robots.txt is checked before every navigation, the home page included. Sub-resources and the
  target of an HTTP redirect are not checked separately.
- A robots.txt `Crawl-delay` is enforced by one limiter shared by all workers: at most one page
  load starts per Crawl-delay, whatever the number of workers. In headless mode a page load is a
  single request (only the HTML document: images, fonts, media, stylesheets and scripts are
  blocked, the extraction reads the DOM only). In headed mode the page also loads its assets.
- Pagination never revisits a URL already queued, so a "next" link that loops ends the category.
- Extraction is one `evaluate` call per page (no per-element round trips).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import BrowserContext, Page, Route
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import ValidationError

from scraper import REPO_URL, TOOL_NAME, __version__
from scraper.log import event
from scraper.models import Book, Category
from scraper.robots import RobotsDisallowed, RobotsPolicy

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/145.0 Safari/537.36 {TOOL_NAME}/{__version__} (+{REPO_URL})"
)

# Runs inside the page: returns plain dicts, validation happens in Python (pydantic).
EXTRACT_JS = """
() => Array.from(document.querySelectorAll('article.product_pod')).map(pod => {
  const a = pod.querySelector('h3 a');
  const text = sel => { const el = pod.querySelector(sel); return el ? el.textContent : null; };
  const star = pod.querySelector('p.star-rating');
  return {
    title: a ? (a.getAttribute('title') || a.textContent) : null,
    url: a ? a.href : null,
    price: text('p.price_color'),
    availability: text('p.availability'),
    rating: star ? star.className : null,
  };
})
"""

PAGER_JS = """
() => {
  const cur = document.querySelector('ul.pager li.current');
  const next = document.querySelector('ul.pager li.next a');
  return { current: cur ? cur.textContent.trim() : null, next: next ? next.href : null };
}
"""

# Resource types that never reach the network in headless mode. The extraction reads the DOM only,
# so the site serves one HTML document per page load. context.route() also disables the HTTP
# cache, so without this every page would download the same CSS and JS again. For a site that
# renders its content with JavaScript, remove "script" from this set.
BLOCKED_RESOURCE_TYPES = frozenset({"image", "font", "media", "stylesheet", "script"})

CATEGORIES_JS = """
() => Array.from(document.querySelectorAll('div.side_categories ul li ul li a'))
        .map(a => ({ name: a.textContent.trim(), url: a.href }))
"""


class TransientHTTPError(RuntimeError):
    """5xx / 429: worth retrying."""


class PermanentHTTPError(RuntimeError):
    """404 and other 4xx: retrying will not help."""


@dataclass
class CrawlSettings:
    base_url: str = "https://books.toscrape.com/"
    concurrency: int = 4
    delay_s: float = 0.5
    max_pages: Optional[int] = None  # per category; None = follow pagination to the end
    attempts: int = 3  # tries per page, the first one included (3 = up to 2 retries)
    backoff_base_s: float = 1.0
    nav_timeout_ms: int = 20_000
    block_assets: bool = True  # headless: HTML only (BLOCKED_RESOURCE_TYPES), less load on the target
    wait_until: str = "domcontentloaded"
    user_agent: str = USER_AGENT


@dataclass
class PageTask:
    category: Category
    url: str
    page_no: int


@dataclass
class PageEvent:
    category: str
    page_no: int
    url: str
    status: str  # ok | failed | skipped_robots
    total_pages: Optional[int] = None
    http_status: Optional[int] = None
    items: int = 0
    valid: int = 0
    invalid: int = 0
    attempts: int = 0
    elapsed_ms: int = 0
    error: Optional[str] = None


@dataclass
class CrawlResult:
    books: list[Book] = field(default_factory=list)
    pages: list[PageEvent] = field(default_factory=list)
    validation_errors: list[dict[str, Any]] = field(default_factory=list)
    retries: int = 0
    workers: int = 0  # browser pages actually used: min(concurrency, categories)
    # categories that still had a "next" page when --max-pages stopped them (the run covers only
    # the first max_pages listing pages of those categories)
    stopped_at_max_pages: list[str] = field(default_factory=list)

    @property
    def pages_ok(self) -> int:
        return sum(1 for p in self.pages if p.status == "ok")

    @property
    def pages_failed(self) -> int:
        return sum(1 for p in self.pages if p.status == "failed")

    def pages_as_dicts(self) -> list[dict[str, Any]]:
        return [asdict(p) for p in self.pages]


# Optional callback used by the demo recorder (HUD overlay, screenshots). Not needed in production.
PageHook = Callable[[Page, PageEvent, list[Book]], Awaitable[None]]


class RateGate:
    """Spaces the start of every request by at least `interval` seconds, across all workers.

    Used for a robots.txt Crawl-delay: the per-worker polite delay alone would let N workers hit the
    site N times faster than the delay asks for. Waiters queue on the lock, so they go one by one.
    """

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            # Loop: asyncio.sleep can wake a few ms early (Windows timer ticks are ~15.6 ms), and the
            # Crawl-delay is a minimum. perf_counter has sub-microsecond resolution on every OS.
            while (delay := self._next - time.perf_counter()) > 0:
                await asyncio.sleep(delay)
            self._next = time.perf_counter() + self.interval


class Crawler:
    def __init__(self, context: BrowserContext, settings: CrawlSettings, log: logging.Logger,
                 robots: Optional[RobotsPolicy] = None, on_page: Optional[PageHook] = None) -> None:
        self.context = context
        self.s = settings
        self.log = log
        self.robots = robots
        self.on_page = on_page
        self.result = CrawlResult()
        crawl_delay = robots.crawl_delay_s if robots else None
        self._gate = RateGate(crawl_delay) if crawl_delay else None
        self._cat_items: dict[str, int] = defaultdict(int)
        self._cat_pages: dict[str, int] = defaultdict(int)
        self._queued: set[str] = set()  # every listing URL ever queued: a looping "next" link ends there

    # ------------------------------------------------------------------ setup
    async def install_asset_blocking(self) -> None:
        if not self.s.block_assets:
            return

        async def _block(route: Route) -> None:
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()

        await self.context.route("**/*", _block)

    async def discover_categories(self) -> list[Category]:
        if self.robots and not self.robots.allows(self.s.base_url):
            raise RobotsDisallowed(f"robots.txt disallows {self.s.base_url} for {self.robots.product_token}")
        page = await self.context.new_page()
        try:
            await self._goto_with_retry(page, self.s.base_url, what="home")
            raw = await page.evaluate(CATEGORIES_JS)
        finally:
            await page.close()
        cats = [Category(**c) for c in raw]
        event(self.log, "categories_found", count=len(cats))
        return cats

    # ------------------------------------------------------------------ crawl
    async def crawl(self, categories: list[Category]) -> CrawlResult:
        queue: asyncio.Queue[PageTask] = asyncio.Queue()
        for cat in categories:
            self._queued.add(_url_key(cat.url))
            queue.put_nowait(PageTask(cat, cat.url, 1))

        n_workers = max(1, min(self.s.concurrency, len(categories)))
        self.result.workers = n_workers
        robots_delay = {"crawl_delay_s": self._gate.interval} if self._gate else {}
        event(self.log, "crawl_start", categories=len(categories), concurrency=n_workers,
              delay_s=self.s.delay_s, **robots_delay, max_pages=self.s.max_pages or "all")
        workers = [asyncio.create_task(self._worker(i, queue)) for i in range(n_workers)]
        try:
            await queue.join()
        finally:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        return self.result

    async def _worker(self, idx: int, queue: asyncio.Queue[PageTask]) -> None:
        # The page is opened inside the loop, under the same guard as the work itself: if the browser
        # is gone, every task is still marked done (as a failed page), so crawl()'s queue.join() returns.
        page: Optional[Page] = None
        try:
            while True:
                task = await queue.get()
                try:
                    if page is None or page.is_closed():  # first task, or a crashed tab
                        page = await self.context.new_page()
                    await self._process(page, task, queue)
                except Exception as exc:  # last-resort guard: record the page as failed and keep going
                    error = f"{type(exc).__name__}: {(str(exc).splitlines() or [''])[0][:200]}"
                    self.result.pages.append(PageEvent(category=task.category.name, page_no=task.page_no,
                                                       url=task.url, status="failed", error=error))
                    event(self.log, "worker_error", logging.ERROR, worker=idx, url=task.url, error=error)
                finally:
                    queue.task_done()
                # Politeness delay between requests of the same worker (with jitter).
                await asyncio.sleep(self.s.delay_s + random.uniform(0, self.s.delay_s * 0.5))
        finally:
            if page is not None and not page.is_closed():
                try:
                    await page.close()
                except Exception:  # browser already gone: nothing left to close
                    pass

    async def _process(self, page: Page, task: PageTask, queue: asyncio.Queue[PageTask]) -> None:
        cat = task.category.name
        ev = PageEvent(category=cat, page_no=task.page_no, url=task.url, status="failed")
        t0 = time.perf_counter()

        if self.robots and not self.robots.allows(task.url):
            ev.status = "skipped_robots"
            self.result.pages.append(ev)
            event(self.log, "robots_skip", logging.WARNING, category=cat, url=task.url)
            return

        try:
            http_status, attempts = await self._goto_with_retry(page, task.url, what=f"{cat} p{task.page_no}")
            ev.http_status, ev.attempts = http_status, attempts
            raw_items = await page.evaluate(EXTRACT_JS)
            pager = await page.evaluate(PAGER_JS)
        except Exception as exc:
            ev.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            ev.error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
            self.result.pages.append(ev)
            event(self.log, "page_failed", logging.ERROR, category=cat, page=task.page_no, error=ev.error)
            return

        scraped_at = datetime.now(timezone.utc)
        books: list[Book] = []
        for raw in raw_items:
            try:
                books.append(Book.from_raw(raw, category=cat, listing_page=task.page_no, scraped_at=scraped_at))
            except ValidationError as ve:
                ev.invalid += 1
                self.result.validation_errors.append({
                    "category": cat, "page": task.page_no, "title": raw.get("title"),
                    "errors": [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in ve.errors()],
                })
                event(self.log, "invalid_record", logging.WARNING, category=cat, title=raw.get("title"))

        ev.total_pages = _parse_total_pages(pager.get("current"))
        ev.items, ev.valid, ev.status = len(raw_items), len(books), "ok"
        ev.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        self.result.books.extend(books)
        self.result.pages.append(ev)
        self._cat_items[cat] += len(books)
        self._cat_pages[cat] += 1

        event(self.log, "page_ok", category=cat, page=f"{task.page_no}/{ev.total_pages or 1}",
              items=ev.valid, attempt=attempts, ms=ev.elapsed_ms)

        if self.on_page is not None:
            try:
                await self.on_page(page, ev, books)
            except Exception as exc:  # a broken hook must never break the crawl
                event(self.log, "hook_error", logging.WARNING, error=repr(exc))

        nxt = pager.get("next")
        within_limit = self.s.max_pages is None or task.page_no < self.s.max_pages
        if nxt and within_limit and _url_key(nxt) not in self._queued:
            self._queued.add(_url_key(nxt))
            queue.put_nowait(PageTask(task.category, nxt, task.page_no + 1))
            return
        extra: dict[str, Any] = {}
        if nxt and not within_limit:  # the category has more pages than --max-pages lets the run read
            self.result.stopped_at_max_pages.append(cat)
            extra["stopped_at_max_pages"] = True
        elif nxt:  # the "next" link points back to a page already queued: stop instead of looping
            extra["pagination_loop"] = True
            event(self.log, "pagination_loop", logging.WARNING, category=cat, url=nxt)
        event(self.log, "category_done", category=cat, pages=self._cat_pages[cat], items=self._cat_items[cat],
              **extra)

    async def _goto_with_retry(self, page: Page, url: str, what: str) -> tuple[int, int]:
        """Navigate with retries. Returns (http_status, attempts). Raises on final failure."""
        last_exc: Exception | None = None
        for attempt in range(1, self.s.attempts + 1):
            try:
                if self._gate is not None:  # robots.txt Crawl-delay, shared by all workers
                    await self._gate.wait()
                resp = await page.goto(url, wait_until=self.s.wait_until, timeout=self.s.nav_timeout_ms)
                status = resp.status if resp else 0
                if status == 429 or status >= 500:
                    raise TransientHTTPError(f"HTTP {status}")
                if status >= 400:
                    raise PermanentHTTPError(f"HTTP {status}")
                return status, attempt
            except PermanentHTTPError:
                raise
            except (TransientHTTPError, PlaywrightTimeoutError, PlaywrightError) as exc:
                last_exc = exc
                if attempt == self.s.attempts:
                    break
                wait = self.s.backoff_base_s * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
                self.result.retries += 1
                event(self.log, "retry", logging.WARNING, target=what, attempt=attempt,
                      wait_s=round(wait, 2), error=str(exc).splitlines()[0][:120])
                await asyncio.sleep(wait)
        assert last_exc is not None
        raise last_exc


def _url_key(url: str) -> str:
    """URL without its #fragment: the same listing page, whatever anchor a link carries."""
    return url.split("#", 1)[0]


def _parse_total_pages(text: Optional[str]) -> Optional[int]:
    """'Page 1 of 4' -> 4 (None when the category has a single page)."""
    if not text:
        return None
    m = re.search(r"of\s+(\d+)", text)
    return int(m.group(1)) if m else None
