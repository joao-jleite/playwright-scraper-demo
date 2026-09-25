"""Async Playwright crawler: categories -> paginated listings -> validated `Book` rows.

Design notes
- A fixed pool of N worker pages pulls tasks from an asyncio.Queue, so N is the hard
  concurrency cap (default 4 open pages).
- Pagination is discovered on the fly: a listing page enqueues its "next" page, which
  lets different categories progress in parallel while each category stays ordered.
- Every navigation goes through `_goto_with_retry` (exponential backoff + jitter);
  after every page the worker sleeps `delay_s` (+ jitter) to stay polite.
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
from scraper.robots import RobotsPolicy

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
    retries: int = 3
    backoff_base_s: float = 1.0
    nav_timeout_ms: int = 20_000
    block_assets: bool = True  # skip images/fonts/media: less load on the target, faster runs
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


class Crawler:
    def __init__(self, context: BrowserContext, settings: CrawlSettings, log: logging.Logger,
                 robots: Optional[RobotsPolicy] = None, on_page: Optional[PageHook] = None) -> None:
        self.context = context
        self.s = settings
        self.log = log
        self.robots = robots
        self.on_page = on_page
        self.result = CrawlResult()
        self._cat_items: dict[str, int] = defaultdict(int)
        self._cat_pages: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------ setup
    async def install_asset_blocking(self) -> None:
        if not self.s.block_assets:
            return

        async def _block(route: Route) -> None:
            if route.request.resource_type in {"image", "font", "media"}:
                await route.abort()
            else:
                await route.continue_()

        await self.context.route("**/*", _block)

    async def discover_categories(self) -> list[Category]:
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
            queue.put_nowait(PageTask(cat, cat.url, 1))

        n_workers = max(1, min(self.s.concurrency, len(categories)))
        event(self.log, "crawl_start", categories=len(categories), concurrency=n_workers,
              delay_s=self.s.delay_s, max_pages=self.s.max_pages or "all")
        workers = [asyncio.create_task(self._worker(i, queue)) for i in range(n_workers)]
        try:
            await queue.join()
        finally:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        return self.result

    async def _worker(self, idx: int, queue: asyncio.Queue[PageTask]) -> None:
        page = await self.context.new_page()
        try:
            while True:
                task = await queue.get()
                try:
                    if page.is_closed():  # a crashed tab should not kill the worker
                        page = await self.context.new_page()
                    await self._process(page, task, queue)
                except Exception as exc:  # last-resort guard: record and keep going
                    event(self.log, "worker_error", logging.ERROR, worker=idx, url=task.url, error=repr(exc))
                finally:
                    queue.task_done()
                # Politeness delay between requests of the same worker (with jitter).
                await asyncio.sleep(self.s.delay_s + random.uniform(0, self.s.delay_s * 0.5))
        finally:
            if not page.is_closed():
                await page.close()

    async def _process(self, page: Page, task: PageTask, queue: asyncio.Queue[PageTask]) -> None:
        cat = task.category.name
        ev = PageEvent(category=cat, page_no=task.page_no, url=task.url, status="failed")
        t0 = time.perf_counter()

        if self.robots and not self.robots.allows(task.url, self.s.user_agent):
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

        within_limit = self.s.max_pages is None or task.page_no < self.s.max_pages
        if pager.get("next") and within_limit:
            queue.put_nowait(PageTask(task.category, pager["next"], task.page_no + 1))
        else:
            event(self.log, "category_done", category=cat, pages=self._cat_pages[cat], items=self._cat_items[cat])

    async def _goto_with_retry(self, page: Page, url: str, what: str) -> tuple[int, int]:
        """Navigate with retries. Returns (http_status, attempts). Raises on final failure."""
        last_exc: Exception | None = None
        for attempt in range(1, self.s.retries + 1):
            try:
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
                if attempt == self.s.retries:
                    break
                wait = self.s.backoff_base_s * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
                self.result.retries += 1
                event(self.log, "retry", logging.WARNING, target=what, attempt=attempt,
                      wait_s=round(wait, 2), error=str(exc).splitlines()[0][:120])
                await asyncio.sleep(wait)
        assert last_exc is not None
        raise last_exc


def _parse_total_pages(text: Optional[str]) -> Optional[int]:
    """'Page 1 of 4' -> 4 (None when the category has a single page)."""
    if not text:
        return None
    m = re.search(r"of\s+(\d+)", text)
    return int(m.group(1)) if m else None
