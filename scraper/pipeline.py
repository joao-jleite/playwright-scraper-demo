"""End-to-end run: robots.txt -> categories -> crawl -> validate -> CSV/XLSX/PDF/run_log.json."""

from __future__ import annotations

import csv
import json
import logging
import platform
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import BrowserContext, async_playwright

from scraper import TOOL_NAME, __version__
from scraper.analysis import dedupe, find_opportunities, kpis, summarize_by_category
from scraper.charts import price_by_category, rating_distribution
from scraper.crawler import Crawler, CrawlResult, CrawlSettings, PageHook
from scraper.excel import write_xlsx
from scraper.log import event, setup_logging
from scraper.models import Book, Category
from scraper.pdf_report import ReportInput, write_pdf
from scraper.robots import RobotsPolicy, fetch_robots

CSV_COLUMNS = ["title", "category", "price_gbp", "rating", "availability", "in_stock", "url", "scraped_at"]


class CategorySelectionError(ValueError):
    pass


@dataclass
class RunSettings:
    out_dir: Path = Path("output")
    categories: Optional[str] = None  # None = all | "5" = first five | "Travel,Mystery" = by name
    headed: bool = False
    slow_mo_ms: int = 0
    log_level: str = "INFO"
    crawl: CrawlSettings = field(default_factory=CrawlSettings)


@dataclass
class ScrapeOutcome:
    robots: RobotsPolicy
    available: list[Category]
    selected: list[Category]
    result: CrawlResult
    started_at: datetime
    crawl_seconds: float


def select_categories(available: list[Category], spec: Optional[str]) -> list[Category]:
    """`None` -> all, `"3"` -> first three (site order), `"Travel, Poetry"` -> by name (case-insensitive)."""
    if not spec or not spec.strip():
        return available
    spec = spec.strip()
    if spec.isdigit():
        n = int(spec)
        if n < 1:
            raise CategorySelectionError("--categories N must be >= 1")
        return available[:n]
    lookup = {c.name.lower(): c for c in available}
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    missing = [w for w in wanted if w.lower() not in lookup]
    if missing:
        names = ", ".join(c.name for c in available)
        raise CategorySelectionError(f"unknown categories: {missing}. Available: {names}")
    return [lookup[w.lower()] for w in wanted]


async def scrape(context: BrowserContext, settings: RunSettings, log: logging.Logger,
                 on_page: Optional[PageHook] = None) -> ScrapeOutcome:
    started_at = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    cs = settings.crawl

    robots = await fetch_robots(context.request, cs.base_url, cs.user_agent)
    if robots.crawl_delay_s and robots.crawl_delay_s > cs.delay_s:
        cs.delay_s = robots.crawl_delay_s  # the site's Crawl-delay always wins
    event(log, "robots_checked", status=robots.http_status, rule=robots.summary.split(":")[0])

    crawler = Crawler(context, cs, log, robots=robots, on_page=on_page)
    await crawler.install_asset_blocking()
    available = await crawler.discover_categories()
    selected = select_categories(available, settings.categories)
    result = await crawler.crawl(selected)
    return ScrapeOutcome(robots, available, selected, result, started_at, time.perf_counter() - t0)


def _write_csv(path: Path, books: list[Book]) -> Path:
    # utf-8-sig: Excel opens accents and £ correctly on double-click
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for b in books:
            writer.writerow(b.as_row())
    return path


def build_outputs(outcome: ScrapeOutcome, settings: RunSettings, log: logging.Logger,
                  wall_t0: float) -> dict[str, Any]:
    """Write every deliverable and return the run_log dict (also saved as run_log.json)."""
    out = settings.out_dir
    out.mkdir(parents=True, exist_ok=True)
    res = outcome.result
    books, duplicates = dedupe(res.books)
    opps = find_opportunities(books)
    summary = summarize_by_category(books, opps)
    k = kpis(books, opps)
    collected = outcome.started_at.strftime("%Y-%m-%d %H:%M UTC")
    outputs: dict[str, str] = {}

    if duplicates:
        event(log, "duplicates_removed", logging.WARNING, count=duplicates)

    outputs["csv"] = _write_csv(out / "books.csv", books).name
    event(log, "output_written", file="books.csv", rows=len(books))

    if books:
        c1 = price_by_category(summary, k["avg_price"], out / "charts" / "avg_price_by_category.png")
        c2 = rating_distribution(k["rating_distribution"], out / "charts" / "rating_distribution.png")
        outputs["charts"] = [f"charts/{c1.name}", f"charts/{c2.name}"]

        run_table = [
            ("Source", settings.crawl.base_url),
            ("Collected at", collected),
            ("Crawl duration", f"{outcome.crawl_seconds:.1f} s"),
            ("Categories crawled", f"{len(outcome.selected)} of {len(outcome.available)}"),
            ("Listing pages", f"{res.pages_ok} OK, {res.pages_failed} failed, {res.retries} retries"),
            ("Records", f"{len(books)} valid, {len(res.validation_errors)} invalid, {duplicates} duplicates"),
            ("robots.txt", outcome.robots.summary),
            ("Politeness", f"{settings.crawl.concurrency} concurrent pages, {settings.crawl.delay_s:.1f} s delay "
                           f"+ jitter, {settings.crawl.retries} attempts with exponential backoff"),
            ("Tool", f"{TOOL_NAME} {__version__} (Python {platform.python_version()}, "
                     f"Playwright {pkg_version('playwright')})"),
        ]
        write_xlsx(out / "books.xlsx", books, summary, opps, {
            "source": "books.toscrape.com", "collected_at": collected, "table": run_table,
            "median_price": k["median_price"], "avg_rating": k["avg_rating"], "in_stock_pct": k["in_stock_pct"],
        })
        outputs["xlsx"] = "books.xlsx"
        event(log, "output_written", file="books.xlsx", sheets="Data,Summary,Opportunities,Run Info")

        write_pdf(out / "report.pdf", ReportInput(
            collected_at=collected, source="books.toscrape.com", robots_summary=outcome.robots.summary,
            duration_s=outcome.crawl_seconds, pages_ok=res.pages_ok, pages_failed=res.pages_failed,
            retries=res.retries, invalid_records=len(res.validation_errors), duplicates=duplicates,
            concurrency=settings.crawl.concurrency, delay_s=settings.crawl.delay_s, kpis=k, summary=summary,
            opportunities=opps, chart_price=c1, chart_rating=c2, categories_crawled=len(outcome.selected),
            categories_available=len(outcome.available), headed=settings.headed))
        outputs["pdf"] = "report.pdf"
        event(log, "output_written", file="report.pdf", pages=3)
    else:
        event(log, "no_data", logging.ERROR, detail="no valid records; XLSX/PDF skipped")

    status = "failed" if not books else ("partial" if res.pages_failed or res.validation_errors else "success")
    run_log: dict[str, Any] = {
        "tool": TOOL_NAME,
        "version": __version__,
        "status": status,
        "started_at": outcome.started_at.isoformat(timespec="seconds"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "crawl_duration_s": round(outcome.crawl_seconds, 2),
        "total_duration_s": round(time.perf_counter() - wall_t0, 2),
        "settings": {
            **{k_: v for k_, v in asdict(settings.crawl).items() if k_ != "user_agent"},
            "user_agent": settings.crawl.user_agent,
            "categories_arg": settings.categories,
            "headed": settings.headed,
        },
        "robots_txt": outcome.robots.as_dict(),
        "categories": {"available": len(outcome.available), "crawled": len(outcome.selected)},
        "pages": {"ok": res.pages_ok, "failed": res.pages_failed, "retries": res.retries,
                  "skipped_by_robots": sum(1 for p in res.pages if p.status == "skipped_robots")},
        "records": {"extracted": sum(p.items for p in res.pages), "valid": len(res.books),
                    "invalid": len(res.validation_errors), "duplicates_removed": duplicates,
                    "exported": len(books)},
        "kpis": {**k, "rating_distribution": {str(s): n for s, n in k["rating_distribution"].items()}},
        "per_category": {s.category: s.titles for s in summary},
        "errors": [{"category": p.category, "page": p.page_no, "url": p.url, "error": p.error}
                   for p in res.pages if p.status == "failed"],
        "validation_errors": res.validation_errors,
        "outputs": {**outputs, "run_log": "run_log.json", "events": "run_events.jsonl"},
        "environment": {"python": platform.python_version(), "playwright": pkg_version("playwright"),
                        "os": platform.system()},
        "page_events": res.pages_as_dicts(),
    }
    (out / "run_log.json").write_text(json.dumps(run_log, indent=2, ensure_ascii=False), encoding="utf-8")
    event(log, "output_written", file="run_log.json", status=status)
    event(log, "run_done", status=status, products=len(books), pages=res.pages_ok, errors=res.pages_failed,
          seconds=round(outcome.crawl_seconds, 1))
    return run_log


async def run(settings: RunSettings) -> dict[str, Any]:
    """CLI entry point: launches its own browser. The demo recorder calls scrape()/build_outputs() directly."""
    wall_t0 = time.perf_counter()
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    log = setup_logging(settings.out_dir / "run_events.jsonl", settings.log_level)
    event(log, "run_start", tool=f"{TOOL_NAME}/{__version__}", out=str(settings.out_dir),
          categories=settings.categories or "all")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not settings.headed, slow_mo=settings.slow_mo_ms)
        context = await browser.new_context(user_agent=settings.crawl.user_agent, locale="en-GB",
                                            viewport={"width": 1280, "height": 800})
        try:
            outcome = await scrape(context, settings, log)
        finally:
            await context.close()
            await browser.close()
    return build_outputs(outcome, settings, log, wall_t0)
