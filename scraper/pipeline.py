"""End-to-end run: robots.txt -> categories -> crawl -> validate -> CSV/XLSX/PDF/run_log.json.

Output safety (the tool is meant to run unattended, e.g. from a daily scheduled task):
- the deliverables (books.xlsx, books.csv, report.pdf, charts/) are first written to a staging
  folder inside --out, then swapped in together;
- if an old deliverable cannot be replaced (typically books.xlsx open in Excel on Windows), none
  is: the previous deliverables stay exactly as they were;
- run_log.json and run_events.jsonl always describe the latest run. Any failure after the
  arguments were validated writes run_log.json with status "failed" and the reason, and Ctrl+C
  writes status "interrupted", so a scheduler never finds a stale "success" next to a run that
  did not finish.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import platform
import shutil
import tempfile
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
from scraper.excel import csv_safe, write_xlsx
from scraper.log import event, open_event_file, setup_logging
from scraper.models import Book, Category
from scraper.pdf_report import ReportInput, write_pdf
from scraper.robots import RobotsPolicy, fetch_robots

CSV_COLUMNS = ["title", "category", "price_gbp", "rating", "availability", "in_stock", "url", "scraped_at"]
EVENTS_FILE = "run_events.jsonl"
RUN_LOG_FILE = "run_log.json"
NO_DELIVERABLES_NOTE = ("This run did not replace any deliverable: files from a previous run, if any, were "
                        "left unchanged.")


class CategorySelectionError(ValueError):
    """Bad --categories value. Raised before anything is written to the output folder."""


class OutputLockedError(RuntimeError):
    """An existing output file cannot be replaced (e.g. books.xlsx open in Excel)."""

    def __init__(self, name: str, out_dir: Path) -> None:
        super().__init__(f"{name} is open in another program (close it and run again); "
                         f"the previous deliverables in {out_dir} were left unchanged")
        self.name = name


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


def check_category_spec(spec: Optional[str]) -> None:
    """Syntax check of --categories, done before the browser starts (names are checked later)."""
    if spec is None:
        return
    s = spec.strip()
    if s.isdigit() and int(s) < 1:
        raise CategorySelectionError("--categories N must be >= 1")
    if not s or not any(part.strip() for part in s.split(",")):
        raise CategorySelectionError("--categories is empty")


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
    # "Travel,travel" names one category: crawl it once (first mention keeps its position)
    return list({w.lower(): lookup[w.lower()] for w in wanted}.values())


def describe_error(exc: BaseException) -> str:
    """One readable line for the console and run_log.json."""
    text = str(exc).strip()
    if "Executable doesn't exist" in text:  # Playwright browser missing
        return "Chromium for Playwright is not installed; run: python -m playwright install chromium"
    if isinstance(exc, OutputLockedError):
        return text
    first = text.splitlines()[0] if text else ""
    return f"{type(exc).__name__}: {first}"[:300] if first else type(exc).__name__


async def scrape(context: BrowserContext, settings: RunSettings, log: logging.Logger,
                 on_page: Optional[PageHook] = None) -> ScrapeOutcome:
    started_at = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    cs = settings.crawl

    robots = await fetch_robots(context.request, cs.base_url)
    event(log, "robots_checked", status=robots.http_status, group=robots.group,
          rules=len(robots.rules.rules) if robots.rules else 0, crawl_delay_s=robots.crawl_delay_s or 0)

    crawler = Crawler(context, cs, log, robots=robots, on_page=on_page)
    await crawler.install_asset_blocking()
    available = await crawler.discover_categories()
    selected = select_categories(available, settings.categories)
    # The arguments are valid from here on: the output folder and the event file may be created.
    open_event_file(log, settings.out_dir / EVENTS_FILE)
    result = await crawler.crawl(selected)
    return ScrapeOutcome(robots, available, selected, result, started_at, time.perf_counter() - t0)


def _write_csv(path: Path, books: list[Book]) -> Path:
    # utf-8-sig: Excel opens accents and £ correctly on double-click
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for b in books:
            writer.writerow({k: csv_safe(v) for k, v in b.as_row().items()})
    return path


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _swap_in(stage: Path, out: Path, names: list[str]) -> None:
    """Move the staged files into `out`, all or nothing.

    Old files are first moved aside into the staging folder. On Windows a file open in Excel cannot
    be moved (PermissionError), so a locked file is detected before anything new is put in place,
    and the files already moved aside are put back.
    """
    aside = stage / ".previous"
    moved: list[str] = []
    try:
        for name in names:
            target = out / name
            if not target.exists():
                continue
            try:
                (aside / name).parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, aside / name)
            except PermissionError as exc:
                raise OutputLockedError(name, out) from exc
            moved.append(name)
    except BaseException:  # locked file, or Ctrl+C in the middle: put the previous set back as it was
        for done in reversed(moved):
            os.replace(aside / done, out / done)
        raise
    for name in names:
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage / name, out / name)


def _settings_dict(settings: RunSettings) -> dict[str, Any]:
    return {
        **asdict(settings.crawl),
        "categories_arg": settings.categories,
        "headed": settings.headed,
    }


def _environment() -> dict[str, str]:
    return {"python": platform.python_version(), "playwright": pkg_version("playwright"), "os": platform.system()}


def page_limit_note(max_pages: Optional[int], stopped: list[str]) -> Optional[str]:
    """One sentence for the reports when --max-pages was used (None for a full crawl).

    A run limited by --max-pages still ends with status "success" (the limit was asked for), so every
    deliverable says that its counts only cover the listing pages that were read.
    """
    if max_pages is None:
        return None
    pages = ("listing page of each category was" if max_pages == 1
             else f"{max_pages} listing pages of each category were")
    note = f"--max-pages {max_pages}: only the first {pages} read"
    if not stopped:
        return note + "; no category had more pages, so the counts are complete."
    if len(stopped) == 1:
        return note + f"; {stopped[0]} has more pages that were not crawled, so its count is partial."
    return note + (f"; {len(stopped)} categories have more pages that were not crawled, so their "
                   "counts are partial.")


def is_interruption(exc: BaseException) -> bool:
    """Ctrl+C: KeyboardInterrupt, or the CancelledError asyncio.run() raises inside the main task."""
    return isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError))


def write_failure_log(settings: RunSettings, log: logging.Logger, exc: BaseException, stage: str,
                      started_at: datetime, wall_t0: float, partial: Optional[dict[str, Any]] = None) -> dict:
    """Record a failed or interrupted run in run_log.json (best effort: never hides the original error)."""
    out = settings.out_dir
    interrupted = is_interruption(exc)
    base = partial or {
        "tool": TOOL_NAME,
        "version": __version__,
        "started_at": started_at.isoformat(timespec="seconds"),
        "settings": _settings_dict(settings),
        "environment": _environment(),
    }
    if "records" in base:  # nothing from this run was delivered, whatever the crawl collected
        base = {**base, "records": {**base["records"], "exported": 0}}
    run_log = {
        **base,
        "status": "interrupted" if interrupted else "failed",
        "failed_stage": stage,
        "error": "interrupted by the user (Ctrl+C)" if interrupted else describe_error(exc),
        "note": NO_DELIVERABLES_NOTE,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_duration_s": round(time.perf_counter() - wall_t0, 2),
        "outputs": {"run_log": RUN_LOG_FILE, "events": EVENTS_FILE},
    }
    try:
        out.mkdir(parents=True, exist_ok=True)
        open_event_file(log, out / EVENTS_FILE)
        event(log, "run_interrupted" if interrupted else "run_failed", logging.ERROR, stage=stage,
              error=run_log["error"])
        _write_json(out / RUN_LOG_FILE, run_log)
    except OSError as write_exc:  # e.g. run_log.json itself locked: the console error is still shown
        event(log, "run_log_not_written", logging.ERROR, error=repr(write_exc))
    return run_log


def build_outputs(outcome: ScrapeOutcome, settings: RunSettings, log: logging.Logger,
                  wall_t0: float) -> dict[str, Any]:
    """Write every deliverable and return the run_log dict (also saved as run_log.json)."""
    out = settings.out_dir
    out.mkdir(parents=True, exist_ok=True)
    res = outcome.result
    cs = settings.crawl
    books, duplicates = dedupe(res.books)
    opps = find_opportunities(books)
    summary = summarize_by_category(books, opps)
    k = kpis(books, opps)
    collected = outcome.started_at.strftime("%Y-%m-%d %H:%M UTC")
    limit_note = page_limit_note(cs.max_pages, res.stopped_at_max_pages)
    outputs: dict[str, Any] = {}
    staged: list[str] = []
    stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=out))
    run_log: dict[str, Any] = {}

    if duplicates:
        event(log, "duplicates_removed", logging.WARNING, count=duplicates)

    try:
        if books:
            outputs["csv"] = _write_csv(stage / "books.csv", books).name
            staged.append("books.csv")
            c1 = price_by_category(summary, k["avg_price"], stage / "charts" / "avg_price_by_category.png")
            c2 = rating_distribution(k["rating_distribution"], stage / "charts" / "rating_distribution.png")
            outputs["charts"] = [f"charts/{c1.name}", f"charts/{c2.name}"]
            staged += outputs["charts"]

            crawl_delay = outcome.robots.crawl_delay_s
            politeness = (f"{res.workers} browser page{'s' if res.workers != 1 else ''} at a time, "
                          f"{cs.delay_s:.1f} s delay + jitter after each page, {cs.attempts} attempts per page "
                          "with exponential backoff")
            if crawl_delay:
                politeness += f"; robots.txt Crawl-delay {crawl_delay:g} s enforced across all pages"
            run_table = [
                ("Source", cs.base_url),
                ("Collected at", collected),
                ("Crawl duration", f"{outcome.crawl_seconds:.1f} s"),
                ("Categories crawled", f"{len(outcome.selected)} of {len(outcome.available)}"),
                *([("Page limit", limit_note)] if limit_note else []),
                ("Listing pages", f"{res.pages_ok} OK, {res.pages_failed} failed, {res.retries} retries"),
                ("Records", f"{len(books)} valid, {len(res.validation_errors)} invalid, {duplicates} duplicates"),
                ("robots.txt", outcome.robots.summary),
                ("Politeness", politeness),
                ("Tool", f"{TOOL_NAME} {__version__} (Python {platform.python_version()}, "
                         f"Playwright {pkg_version('playwright')})"),
            ]
            write_xlsx(stage / "books.xlsx", books, summary, opps, {
                "source": "books.toscrape.com", "collected_at": collected, "table": run_table,
                "median_price": k["median_price"], "avg_rating": k["avg_rating"], "in_stock_pct": k["in_stock_pct"],
                "limit_note": limit_note,
            })
            outputs["xlsx"] = "books.xlsx"
            staged.append("books.xlsx")
            event(log, "output_ready", file="books.xlsx", sheets="Data,Summary,Opportunities,Run Info")

            pdf_pages = write_pdf(stage / "report.pdf", ReportInput(
                collected_at=collected, source="books.toscrape.com", robots_summary=outcome.robots.summary,
                duration_s=outcome.crawl_seconds, pages_ok=res.pages_ok, pages_failed=res.pages_failed,
                retries=res.retries, invalid_records=len(res.validation_errors), duplicates=duplicates,
                concurrency=res.workers, delay_s=cs.delay_s, kpis=k, summary=summary,
                opportunities=opps, chart_price=c1, chart_rating=c2, categories_crawled=len(outcome.selected),
                categories_available=len(outcome.available), headed=settings.headed, crawl_delay_s=crawl_delay,
                max_pages=cs.max_pages, limit_note=limit_note))
            outputs["pdf"] = "report.pdf"
            staged.append("report.pdf")
            event(log, "output_ready", file="report.pdf", pages=pdf_pages)
        else:
            # Nothing to deliver: keep the previous deliverables untouched instead of mixing an
            # empty CSV with an older workbook and report. run_log.json says what happened.
            event(log, "no_data", logging.ERROR, detail="no valid records; no deliverable written")

        # partial = some listing pages failed or some records did not pass validation
        status = "failed" if not books else ("partial" if res.pages_failed or res.validation_errors else "success")
        run_log = {
            "tool": TOOL_NAME,
            "version": __version__,
            "status": status,
            "started_at": outcome.started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "crawl_duration_s": round(outcome.crawl_seconds, 2),
            "total_duration_s": round(time.perf_counter() - wall_t0, 2),
            "settings": {**_settings_dict(settings), "workers_used": res.workers},
            "robots_txt": outcome.robots.as_dict(),
            "categories": {"available": len(outcome.available), "crawled": len(outcome.selected),
                           # names of the categories --max-pages stopped before their last page
                           "stopped_at_max_pages": res.stopped_at_max_pages},
            "page_limit_note": limit_note,
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
            "outputs": {**outputs, "run_log": RUN_LOG_FILE, "events": EVENTS_FILE},
            "environment": _environment(),
            "page_events": res.pages_as_dicts(),
        }
        if status == "failed":
            run_log["note"] = NO_DELIVERABLES_NOTE
        _write_json(stage / RUN_LOG_FILE, run_log)
        staged.append(RUN_LOG_FILE)
        _swap_in(stage, out, staged)
    except BaseException as exc:  # Ctrl+C included: run_log.json then says "interrupted"
        partial = {key: v for key, v in run_log.items() if key not in ("status", "outputs")} or None
        write_failure_log(settings, log, exc, "outputs", outcome.started_at, wall_t0, partial)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)  # our own temporary folder (and the replaced old files)

    for name in staged + [EVENTS_FILE]:  # the event file is written in place, event by event
        event(log, "output_written", file=name)
    event(log, "run_done", status=status, products=len(books), pages=res.pages_ok, errors=res.pages_failed,
          seconds=round(outcome.crawl_seconds, 1))
    return run_log


async def run(settings: RunSettings) -> dict[str, Any]:
    """CLI entry point: launches its own browser. The demo recorder calls scrape()/build_outputs() directly."""
    wall_t0 = time.perf_counter()
    started_at = datetime.now(timezone.utc)
    # The event file is opened by scrape() once --categories is known to be valid: a rejected
    # argument leaves no output folder behind.
    log = setup_logging(level=settings.log_level, deferred=True)
    event(log, "run_start", tool=f"{TOOL_NAME}/{__version__}", out=str(settings.out_dir),
          categories=settings.categories or "all")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not settings.headed, slow_mo=settings.slow_mo_ms)
            try:
                context = await browser.new_context(user_agent=settings.crawl.user_agent, locale="en-GB",
                                                    viewport={"width": 1280, "height": 800})
                outcome = await scrape(context, settings, log)
            finally:
                await browser.close()
    except CategorySelectionError:
        raise  # bad argument: nothing written
    except BaseException as exc:  # errors, and Ctrl+C (CancelledError here): run_log.json says why
        write_failure_log(settings, log, exc, "crawl", started_at, wall_t0)
        raise
    return build_outputs(outcome, settings, log, wall_t0)
