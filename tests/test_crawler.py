"""Crawler networking: retry with backoff, 4xx not retried, Crawl-delay limiter (no network).

Responses come from Playwright routing (`context.route` + `route.fulfill`), so the real
`page.goto` / status handling runs against a real Chromium page.
"""

import asyncio
import logging
import time

import pytest
from playwright.async_api import Route, async_playwright

from scraper.crawler import Crawler, CrawlSettings, PermanentHTTPError, RateGate, TransientHTTPError
from scraper.robots import RobotsPolicy, parse_robots

URL = "https://books.toscrape.com/catalogue/category/books/travel_2/index.html"
OK_HTML = "<html><body><p>ok</p></body></html>"


def _goto(statuses: list[int], attempts: int = 3, robots: RobotsPolicy | None = None):
    """Serve `statuses` in order for URL (last one repeats). Returns (outcome, hits, crawler)."""
    hits: list[int] = []

    async def handler(route: Route) -> None:
        status = statuses[min(len(hits), len(statuses) - 1)]
        hits.append(status)
        await route.fulfill(status=status, body=OK_HTML, content_type="text/html")

    async def go():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            context = await browser.new_context()
            await context.route("**/*", handler)
            settings = CrawlSettings(attempts=attempts, backoff_base_s=0.01)  # fast backoff for tests
            crawler = Crawler(context, settings, logging.getLogger("test"), robots=robots)
            page = await context.new_page()
            try:
                outcome = await crawler._goto_with_retry(page, URL, what="test")
            except Exception as exc:  # returned, not raised, so the test can inspect it
                outcome = exc
            await browser.close()
            return outcome, hits, crawler
    return asyncio.run(go())


def test_transient_errors_are_retried_until_success():
    outcome, hits, crawler = _goto([503, 503, 200])
    assert outcome == (200, 3)  # (http status, attempts)
    assert hits == [503, 503, 200] and crawler.result.retries == 2


def test_404_is_not_retried():
    outcome, hits, crawler = _goto([404, 200])
    assert isinstance(outcome, PermanentHTTPError) and "404" in str(outcome)
    assert hits == [404] and crawler.result.retries == 0


def test_429_is_retried_then_gives_up_after_the_last_attempt():
    outcome, hits, crawler = _goto([429])
    assert isinstance(outcome, TransientHTTPError) and "429" in str(outcome)
    assert hits == [429, 429, 429] and crawler.result.retries == 2


def test_single_attempt_means_no_retry():
    outcome, hits, _ = _goto([500, 200], attempts=1)
    assert isinstance(outcome, TransientHTTPError) and hits == [500]


def test_rate_gate_spaces_requests_across_workers():
    async def go():
        gate, starts = RateGate(0.2), []

        async def worker():
            for _ in range(2):
                await gate.wait()
                starts.append(time.perf_counter())
        await asyncio.gather(*(worker() for _ in range(3)))  # 3 workers, 6 requests
        return sorted(starts)
    starts = asyncio.run(go())
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert len(starts) == 6 and min(gaps) >= 0.2  # never faster than the Crawl-delay, whatever the workers


def test_robots_crawl_delay_enables_the_shared_limiter():
    policy = RobotsPolicy("https://x/robots.txt", 200, "t", parse_robots("User-agent: *\nCrawl-delay: 0.25\n"))
    outcome, hits, crawler = _goto([503, 200], robots=policy)
    assert crawler._gate is not None and crawler._gate.interval == 0.25
    assert outcome == (200, 2)


@pytest.mark.parametrize("n_categories,expected", [(2, 2), (10, 4)])
def test_workers_reported_are_the_ones_actually_used(n_categories, expected):
    from scraper.models import Category

    async def go():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            context = await browser.new_context()

            async def handler(route: Route) -> None:
                await route.fulfill(status=200, body=OK_HTML, content_type="text/html")
            await context.route("**/*", handler)
            crawler = Crawler(context, CrawlSettings(concurrency=4, delay_s=0), logging.getLogger("test"))
            cats = [Category(name=f"C{i}", url=f"https://books.toscrape.com/c{i}/index.html")
                    for i in range(n_categories)]
            result = await crawler.crawl(cats)
            await browser.close()
            return result
    result = asyncio.run(go())
    assert result.workers == expected and result.pages_ok == n_categories
