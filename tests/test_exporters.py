"""Exporters end to end (CSV, XLSX, PDF, run_log.json) from a synthetic crawl result.

No browser and no network: build_outputs() only needs the in-memory outcome of a crawl.
"""

import contextlib
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone

import pytest
from openpyxl import load_workbook

import scraper.pipeline as pipeline
from scraper.analysis import CategorySummary
from scraper.crawler import CrawlResult, CrawlSettings, PageEvent
from scraper.log import setup_logging
from scraper.models import Book, Category
from scraper.pdf_report import _market_sentence, _opportunities_intro, _pct_below
from scraper.pipeline import CSV_COLUMNS, OutputLockedError, RunSettings, ScrapeOutcome, build_outputs
from scraper.robots import RobotsPolicy

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
CATS = [Category(name=n, url=f"https://books.toscrape.com/catalogue/category/books/{n.lower()}_1/index.html")
        for n in ("Travel", "Poetry", "Mystery")]


def _book(title: str, category: str, price: float, rating: int, stock: bool = True) -> Book:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return Book(title=title, category=category, price_gbp=price, rating=rating,
                availability="In stock" if stock else "Out of stock", in_stock=stock,
                url=f"https://books.toscrape.com/catalogue/{slug}_1/index.html",
                listing_page=1, scraped_at=NOW)


def _outcome(books: list[Book], failed_page: bool = False, stopped: tuple[str, ...] = ()) -> ScrapeOutcome:
    pages = [PageEvent(category=c.name, page_no=1, url=c.url, status="ok", items=2, valid=2, attempts=1)
             for c in CATS[:2]]
    if failed_page:
        pages.append(PageEvent(category="Mystery", page_no=1, url=CATS[2].url, status="failed", attempts=3,
                               error="TimeoutError: simulated"))
    result = CrawlResult(books=books, pages=pages, retries=2 if failed_page else 0, workers=3,
                         stopped_at_max_pages=list(stopped))
    robots = RobotsPolicy("https://books.toscrape.com/robots.txt", 404, "no robots.txt (HTTP 404): test")
    return ScrapeOutcome(robots=robots, available=CATS, selected=CATS, result=result, started_at=NOW,
                         crawl_seconds=12.3)


BOOKS = [
    _book("Cheap Gem", "Travel", 12.5, 5),
    _book("Mid Travel", "Travel", 30.0, 3),
    _book("Pricey Travel", "Travel", 55.0, 4),
    _book("Poem Deal", "Poetry", 10.0, 4, stock=False),
    _book("Poem Luxe", "Poetry", 48.0, 2),
]
DELIVERABLES = ["books.csv", "books.xlsx", "report.pdf", "run_log.json",
                "charts/avg_price_by_category.png", "charts/rating_distribution.png"]


def _build(out, books, failed_page=False, max_pages=None, stopped=()):
    settings = RunSettings(out_dir=out, crawl=CrawlSettings(concurrency=4, max_pages=max_pages))
    return build_outputs(_outcome(books, failed_page, stopped), settings, setup_logging(None, "WARNING"),
                         wall_t0=0.0)


@pytest.fixture()
def run(tmp_path):
    """Run the exporters once; a duplicate row and a failed page exercise the 'partial' path."""
    return tmp_path, _build(tmp_path, BOOKS + [BOOKS[0]], failed_page=True)


def test_csv_has_bom_header_and_deduplicated_rows(run):
    out, _ = run
    raw = (out / "books.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # utf-8-sig: Excel opens £ and accents correctly
    rows = list(csv.DictReader((out / "books.csv").open(encoding="utf-8-sig")))
    assert list(rows[0].keys()) == CSV_COLUMNS
    assert len(rows) == len(BOOKS)  # the duplicate URL was dropped
    assert rows[0]["price_gbp"] == "12.5" and rows[0]["rating"] == "5"


def test_xlsx_sheets_tables_and_formats(run):
    out, _ = run
    wb = load_workbook(out / "books.xlsx")
    assert wb.sheetnames == ["Data", "Summary", "Opportunities", "Run Info"]
    # opens on Summary, whose first lines say DEMO; exactly one tab selected (no grouped sheets)
    assert wb.active.title == "Summary" and wb["Summary"]["A2"].value.startswith("DEMO data")
    assert [ws.sheet_view.tabSelected for ws in wb.worksheets] == [False, True, False, False]

    data = wb["Data"]
    assert "tblBooks" in data.tables and data.tables["tblBooks"].ref == f"A1:H{len(BOOKS) + 1}"
    assert data.freeze_panes == "A2"
    assert data["C2"].value == 12.5 and "£" in data["C2"].number_format
    assert data["G2"].hyperlink is not None  # product URL is clickable

    summary = wb["Summary"]
    # Header on row 4, one row per category, sorted by title count (Travel 3, Poetry 2)
    assert [summary.cell(row=r, column=1).value for r in (5, 6)] == ["Travel", "Poetry"]
    assert summary["B5"].value == 3 and summary["C5"].value == round((12.5 + 30 + 55) / 3, 2)
    assert summary["E5"].value == 12.5 and summary["F5"].value == 55.0

    # Opportunity rule: rating >= 4 and price below the category median
    # Travel median 30 -> Cheap Gem (5*, 12.50); Pricey Travel (4*) is above it. Poetry median 29 -> Poem Deal.
    opp = wb["Opportunities"]
    titles = [opp.cell(row=r, column=1).value for r in range(5, opp.max_row + 1)]
    assert titles == ["Cheap Gem", "Poem Deal"]


def test_run_info_reports_the_workers_actually_used(run):
    out, saved = run
    info = {r[0].value: r[1].value for r in load_workbook(out / "books.xlsx")["Run Info"].iter_rows(min_row=4)
            if r[0].value}
    # --concurrency 4, but the crawl only used 3 pages: the report must say 3
    assert info["Politeness"].startswith("3 browser pages at a time")
    assert saved["settings"]["concurrency"] == 4 and saved["settings"]["workers_used"] == 3
    assert saved["settings"]["attempts"] == 3


def test_pdf_is_valid_and_has_three_pages(run):
    out, _ = run
    pdf = (out / "report.pdf").read_bytes()
    assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
    # reportlab writes one "/Type /Page" dictionary per page (the "/Pages" tree node is excluded)
    assert len(re.findall(rb"/Type /Page(?!s)", pdf)) == 3
    assert (out / "charts" / "avg_price_by_category.png").stat().st_size > 5_000
    assert (out / "charts" / "rating_distribution.png").stat().st_size > 5_000


def test_run_log_reports_partial_run(run):
    out, returned = run
    saved = json.loads((out / "run_log.json").read_text(encoding="utf-8"))
    assert saved == returned
    assert saved["status"] == "partial"  # one page failed
    assert saved["pages"] == {"ok": 2, "failed": 1, "retries": 2, "skipped_by_robots": 0}
    assert saved["records"]["duplicates_removed"] == 1 and saved["records"]["exported"] == len(BOOKS)
    assert saved["errors"][0]["error"].startswith("TimeoutError")
    assert saved["kpis"]["opportunities"] == 2
    assert {"csv", "xlsx", "pdf", "run_log"} <= saved["outputs"].keys()
    assert not list(out.glob(".staging-*"))  # the staging folder is always cleaned up


def test_no_valid_records_keeps_previous_deliverables(tmp_path):
    _build(tmp_path, BOOKS)
    before = {n: (tmp_path / n).read_bytes() for n in DELIVERABLES if n != "run_log.json"}
    run_log = _build(tmp_path, [])
    assert run_log["status"] == "failed" and "left unchanged" in run_log["note"]
    assert json.loads((tmp_path / "run_log.json").read_text(encoding="utf-8"))["status"] == "failed"
    # no empty CSV next to an older workbook: the previous deliverables are exactly as they were
    assert {n: (tmp_path / n).read_bytes() for n in before} == before


def test_formula_like_titles_stay_text(tmp_path):
    evil = [_book('=HYPERLINK("https://example.com","click")', "Travel", 20.0, 5),
            _book("+cmd", "Travel", 30.0, 3), _book("Normal", "Travel", 40.0, 1)]
    _build(tmp_path, evil)
    ws = load_workbook(tmp_path / "books.xlsx")["Data"]
    cells = {ws.cell(row=r, column=1).value: ws.cell(row=r, column=1).data_type for r in range(2, 5)}
    assert cells['=HYPERLINK("https://example.com","click")'] == "s"  # a string cell, never a live formula
    rows = list(csv.DictReader((tmp_path / "books.csv").open(encoding="utf-8-sig")))
    titles = {r["title"] for r in rows}
    assert "'=HYPERLINK(\"https://example.com\",\"click\")" in titles and "'+cmd" in titles and "Normal" in titles


@contextlib.contextmanager
def _locked(path, monkeypatch):
    """Hold `path` the way Excel does. On Windows a real open handle blocks renaming the file;
    elsewhere renames are never blocked, so the same PermissionError is simulated."""
    if sys.platform == "win32":
        with open(path, "rb"):
            yield
    else:
        real = os.replace

        def fake(src, dst):
            if os.fspath(src) == os.fspath(path):
                raise PermissionError(13, "locked", os.fspath(src))
            return real(src, dst)
        monkeypatch.setattr(pipeline.os, "replace", fake)
        yield
        monkeypatch.setattr(pipeline.os, "replace", real)


def test_locked_output_changes_nothing_and_logs_failure(tmp_path, monkeypatch):
    _build(tmp_path, BOOKS)
    before = {n: (tmp_path / n).read_bytes() for n in DELIVERABLES if n != "run_log.json"}
    with _locked(tmp_path / "books.xlsx", monkeypatch):
        with pytest.raises(OutputLockedError, match="books.xlsx is open in another program"):
            _build(tmp_path, BOOKS[:3])  # a different result that would change every file
    # all or nothing: every previous deliverable is still the old one...
    assert {n: (tmp_path / n).read_bytes() for n in before} == before
    # ...and run_log.json says the run failed, and why
    saved = json.loads((tmp_path / "run_log.json").read_text(encoding="utf-8"))
    assert saved["status"] == "failed" and saved["failed_stage"] == "outputs"
    assert "books.xlsx is open" in saved["error"]
    # the crawl had 3 valid records, but none of them was delivered
    assert saved["records"]["valid"] == 3 and saved["records"]["exported"] == 0
    assert not list(tmp_path.glob(".staging-*"))


def _summary(names):
    return [CategorySummary(n, 10 - i, 20.0 + i, 10.0, 30.0, 20.0, 3.0, 1.0, 1) for i, n in enumerate(names)]


def test_pdf_wording_follows_the_real_number_of_categories():
    two = _market_sentence(_summary(["Travel", "Poetry"]), 12, 21.0)
    assert two.startswith("Among the 2 categories in this run") and "12" not in two
    many = _market_sentence(_summary([f"C{i}" for i in range(50)]), 12, 21.0)
    assert many.startswith("Among the 12 largest of 50 categories")
    assert _market_sentence(_summary(["Travel"]), 12, 21.0).startswith("This run covers one category")
    assert "6 titles match, all listed below" in _opportunities_intro(6, 6)
    assert "173 titles match; the top 10 are listed below" in _opportunities_intro(173, 10)
    assert "No title matches" in _opportunities_intro(0, 0)
    assert (_pct_below(0.0003), _pct_below(0.253)) == ("<1%", "25%")


def test_max_pages_is_stated_in_every_deliverable(tmp_path):
    saved = _build(tmp_path, BOOKS, max_pages=1, stopped=("Travel",))
    assert saved["status"] == "success"  # the limit was asked for...
    assert saved["categories"]["stopped_at_max_pages"] == ["Travel"]  # ...and what it left out is recorded
    assert "Travel has more pages that were not crawled" in saved["page_limit_note"]
    wb = load_workbook(tmp_path / "books.xlsx")
    assert wb["Summary"]["A3"].value.startswith("Page limit: --max-pages 1")
    info = {r[0].value: r[1].value for r in wb["Run Info"].iter_rows(min_row=4) if r[0].value}
    assert info["Page limit"] == saved["page_limit_note"]
    pdf = (tmp_path / "report.pdf").read_bytes()
    assert len(re.findall(rb"/Type /Page(?!s)", pdf)) == 3  # the extra paragraph does not add a page


def test_full_crawl_has_no_page_limit_note(run):
    out, saved = run
    assert saved["page_limit_note"] is None and saved["categories"]["stopped_at_max_pages"] == []
    assert load_workbook(out / "books.xlsx")["Summary"]["A3"].value is None
