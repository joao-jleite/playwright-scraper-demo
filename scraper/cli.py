"""Command line interface.

Examples
    python -m scraper                                  # full catalog, headless, ./output
    python -m scraper --categories 3 --max-pages 1     # quick smoke run
    python -m scraper --categories "Travel,Poetry" --headed --out demo_out
    python -m scraper --list-categories
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from scraper import __version__
from scraper.crawler import Crawler, CrawlSettings
from scraper.log import setup_logging
from scraper.pipeline import CategorySelectionError, RunSettings, run
from scraper.robots import RobotsUnavailable


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scraper",
        description="Scrape a product catalog (books.toscrape.com sandbox) into Excel, CSV and a PDF report.")
    p.add_argument("--categories", metavar="N|NAMES",
                   help='number of categories (e.g. 5) or comma-separated names (e.g. "Travel,Poetry"). '
                        "Default: all")
    p.add_argument("--max-pages", type=_positive_int, metavar="N", help="max listing pages per category")
    p.add_argument("--concurrency", type=_positive_int, default=4, help="parallel browser pages (default: 4)")
    p.add_argument("--delay", type=float, default=0.5, metavar="SECONDS",
                   help="polite delay after each page, per worker, plus jitter (default: 0.5)")
    p.add_argument("--retries", type=_positive_int, default=3, help="attempts per page (default: 3)")
    p.add_argument("--headed", action="store_true", help="show the browser window (loads images too)")
    p.add_argument("--slow-mo", type=int, default=0, metavar="MS", help="slow down browser actions (debug)")
    p.add_argument("--out", type=Path, default=Path("output"), help="output folder (default: ./output)")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--list-categories", action="store_true", help="print available categories and exit")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


async def _list_categories(settings: CrawlSettings) -> None:
    log = setup_logging(None, "WARNING")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(user_agent=settings.user_agent)
        cats = await Crawler(context, settings, log).discover_categories()
        await browser.close()
    for i, c in enumerate(cats, start=1):
        print(f"{i:>3}. {c.name}")


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; keep £ and accents printable.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)
    crawl = CrawlSettings(concurrency=args.concurrency, delay_s=max(0.0, args.delay), max_pages=args.max_pages,
                          retries=args.retries, block_assets=not args.headed,
                          wait_until="load" if args.headed else "domcontentloaded")
    if args.list_categories:
        asyncio.run(_list_categories(crawl))
        return 0

    settings = RunSettings(out_dir=args.out, categories=args.categories, headed=args.headed,
                           slow_mo_ms=args.slow_mo, log_level=args.log_level, crawl=crawl)
    try:
        run_log = asyncio.run(run(settings))
    except CategorySelectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RobotsUnavailable as exc:
        print(f"error: robots.txt unavailable, refusing to crawl ({exc})", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    rec, pages = run_log["records"], run_log["pages"]
    print(f"\n[{run_log['status'].upper()}] {rec['exported']} products · {run_log['categories']['crawled']} categories"
          f" · {pages['ok']} pages · {pages['failed']} failed · {rec['invalid']} invalid"
          f" · {run_log['crawl_duration_s']:.1f} s")
    for key in ("xlsx", "csv", "pdf", "run_log"):
        if key in run_log["outputs"]:
            print(f"  -> {settings.out_dir / run_log['outputs'][key]}")
    return {"success": 0, "partial": 1}.get(run_log["status"], 2)
