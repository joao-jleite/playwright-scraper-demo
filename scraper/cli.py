"""Command line interface.

Examples
    python -m scraper                                  # full catalog, headless, ./output
    python -m scraper --categories 3 --max-pages 1     # quick smoke run
    python -m scraper --categories "Travel,Poetry" --headed --out demo_out
    python -m scraper --list-categories

Exit codes (for schedulers)
    0    success
    1    partial: some listing pages failed or some records did not pass validation
    2    failed: bad arguments, robots.txt unreachable or disallowing, site/browser error,
         no valid records, or an output file that could not be replaced (e.g. open in Excel)
    130  interrupted (Ctrl+C); run_log.json then records status "interrupted"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from scraper import __version__
from scraper.crawler import Crawler, CrawlSettings
from scraper.log import setup_logging
from scraper.pipeline import (RUN_LOG_FILE, CategorySelectionError, RunSettings, check_category_spec,
                              describe_error, run)
from scraper.robots import RobotsUnavailable, fetch_robots

EXIT_SUCCESS, EXIT_PARTIAL, EXIT_FAILED, EXIT_INTERRUPTED = 0, 1, 2, 130


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def _non_negative_float(value: str) -> float:
    x = float(value)
    if x < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return x


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scraper",
        description="Scrape a product catalog (books.toscrape.com sandbox) into Excel, CSV and a PDF report.",
        epilog="Exit codes: 0 success, 1 partial (failed pages or invalid records), 2 failed or bad arguments, "
               "130 interrupted.")
    p.add_argument("--categories", metavar="N|NAMES",
                   help='number of categories (e.g. 5) or comma-separated names (e.g. "Travel,Poetry"). '
                        "Default: all")
    p.add_argument("--max-pages", type=_positive_int, metavar="N",
                   help="max listing pages per category (the reports then name the categories that had more)")
    p.add_argument("--concurrency", type=_positive_int, default=4,
                   help="max browser pages at a time (default: 4; never more than the number of categories). "
                        "A robots.txt Crawl-delay still limits page loads across all of them")
    p.add_argument("--delay", type=_non_negative_float, default=0.5, metavar="SECONDS",
                   help="polite delay after each page, per worker, plus jitter (default: 0.5). A robots.txt "
                        "Crawl-delay is enforced on top of it, across all workers")
    p.add_argument("--attempts", type=_positive_int, default=3, metavar="N",
                   help="tries per page, the first one included (default: 3 = up to 2 retries)")
    p.add_argument("--headed", action="store_true",
                   help="show the browser window (pages then load their images, styles and scripts too)")
    p.add_argument("--slow-mo", type=int, default=0, metavar="MS", help="slow down browser actions (debug)")
    p.add_argument("--out", type=Path, default=Path("output"), help="output folder (default: ./output)")
    p.add_argument("--base-url", default=CrawlSettings.base_url, metavar="URL",
                   help=f"site root (default: {CrawlSettings.base_url}). The selectors in crawler.py are "
                        "written for that site")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--list-categories", action="store_true", help="print available categories and exit")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def settings_from_args(args: argparse.Namespace) -> RunSettings:
    """CLI arguments -> RunSettings. Also used by scripts/record_demo.py, so the demo runs the exact
    command it shows."""
    crawl = CrawlSettings(base_url=args.base_url, concurrency=args.concurrency, delay_s=args.delay,
                          max_pages=args.max_pages, attempts=args.attempts, block_assets=not args.headed,
                          wait_until="load" if args.headed else "domcontentloaded")
    return RunSettings(out_dir=args.out, categories=args.categories, headed=args.headed,
                       slow_mo_ms=args.slow_mo, log_level=args.log_level, crawl=crawl)


async def _list_categories(settings: CrawlSettings) -> None:
    log = setup_logging(None, "WARNING")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            context = await browser.new_context(user_agent=settings.user_agent)
            robots = await fetch_robots(context.request, settings.base_url)
            cats = await Crawler(context, settings, log, robots=robots).discover_categories()
        finally:
            await browser.close()
    for i, c in enumerate(cats, start=1):
        print(f"{i:>3}. {c.name}")


def _fail(message: str, settings: RunSettings | None = None) -> int:
    print(f"error: {message}", file=sys.stderr)
    if settings is not None and (settings.out_dir / RUN_LOG_FILE).exists():
        print(f"  details: {settings.out_dir / RUN_LOG_FILE}", file=sys.stderr)
    return EXIT_FAILED


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def summary_lines(run_log: dict, out_dir: str, sep: str = os.sep) -> list[str]:
    """The lines printed at the end of a run (also shown in the README demo by scripts/record_demo.py)."""
    rec, pages, cats = run_log["records"], run_log["pages"], run_log["categories"]
    lines = ["", f"[{run_log['status'].upper()}] {_count(rec['exported'], 'product', 'products')}"
                 f" · {_count(cats['crawled'], 'category', 'categories')}"
                 f" · {_count(pages['ok'], 'page', 'pages')} · {pages['failed']} failed · {rec['invalid']} invalid"
                 f" · {run_log['crawl_duration_s']:.1f} s"]
    max_pages = run_log["settings"].get("max_pages")
    if max_pages:  # a limited run: say it next to the numbers, not only in the files
        stopped = len(cats.get("stopped_at_max_pages") or [])
        tail = (f"{_count(stopped, 'category', 'categories')} not read to the end" if stopped
                else "no category had more pages")
        lines.append(f"  page limit: --max-pages {max_pages} ({tail})")
    for key in ("xlsx", "csv", "pdf", "run_log"):
        if key in run_log["outputs"]:
            lines.append(f"  -> {out_dir}{sep}{run_log['outputs'][key]}")
    return lines


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; keep £ and accents printable.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)  # invalid arguments: argparse prints usage and exits with 2
    try:
        check_category_spec(args.categories)  # before the browser starts
    except CategorySelectionError as exc:
        parser.error(str(exc))
    settings = settings_from_args(args)

    if args.list_categories:
        try:
            asyncio.run(_list_categories(settings.crawl))
        except KeyboardInterrupt:
            return EXIT_INTERRUPTED
        except Exception as exc:
            return _fail(describe_error(exc))
        return EXIT_SUCCESS

    try:
        run_log = asyncio.run(run(settings))
    except CategorySelectionError as exc:
        return _fail(str(exc))
    except RobotsUnavailable as exc:
        return _fail(f"robots.txt unavailable, refusing to crawl ({exc})", settings)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        if (settings.out_dir / RUN_LOG_FILE).exists():
            print(f"  details: {settings.out_dir / RUN_LOG_FILE}", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:  # site down, browser missing, file locked...: one line, never a traceback
        return _fail(describe_error(exc), settings)

    print("\n".join(summary_lines(run_log, str(settings.out_dir))))
    return {"success": EXIT_SUCCESS, "partial": EXIT_PARTIAL}.get(run_log["status"], EXIT_FAILED)
