"""CLI exit codes end to end: real Chromium against a local HTTP server (no internet).

The server plays the site: robots.txt, a home page with the category sidebar (the saved real
listing page) and one category listing with a "next" link. It records every path it serves.
"""

import asyncio
import json
import re
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import scraper.pipeline as pipeline
from scraper.cli import main
from scraper.models import Category
from scraper.pipeline import select_categories

# The saved page loads jQuery from a CDN: point every absolute URL at the local server (404) so
# the suite never leaves localhost.
LISTING = re.sub(rb'(src|href)="(https?:)?//[^"]*"', rb'\1="/external-blocked"',
                 (Path(__file__).parent / "fixtures" / "mystery_page1.html").read_bytes())


class Routes(dict):
    """path -> (status, body), plus `hits`: every path requested, in order."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.hits: list[str] = []


class _Site(BaseHTTPRequestHandler):
    routes: Routes = Routes()

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        self.routes.hits.append(self.path)
        status, body = self.routes.get(self.path, (404, b"not found"))
        self.send_response(status)
        self.send_header("Content-Type", "text/plain" if self.path.endswith(".txt") else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # keep pytest output clean
        pass


@pytest.fixture()
def site():
    """Local site; tests edit `routes`. Default: no robots.txt, home + Travel page 1 OK, page 2 missing."""
    routes = Routes({"/": (200, LISTING), "/travel_2/index.html": (200, LISTING)})
    handler = type("Site", (_Site,), {"routes": routes})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/", routes
    server.shutdown()
    server.server_close()


def _cli(base_url: str, out: Path, *extra: str) -> int:
    return main(["--base-url", base_url, "--out", str(out), "--delay", "0", "--attempts", "1", *extra])


def _run_log(out: Path) -> dict:
    return json.loads((out / "run_log.json").read_text(encoding="utf-8"))


def test_bad_category_number_is_rejected_before_the_browser_starts(tmp_path, capsys):
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as exc:
        main(["--categories", "0", "--out", str(out)])
    assert exc.value.code == 2 and "must be >= 1" in capsys.readouterr().err
    assert not out.exists()


def test_unknown_category_exits_2_and_leaves_no_output_folder(site, tmp_path, capsys):
    url, _ = site
    out = tmp_path / "out"
    assert _cli(url, out, "--categories", "Cooking") == 2
    assert "unknown categories: ['Cooking']" in capsys.readouterr().err
    assert not out.exists()


def test_home_page_503_exits_2_with_a_failed_run_log(site, tmp_path, capsys):
    url, routes = site
    routes["/"] = (503, b"down")
    out = tmp_path / "out"
    assert _cli(url, out) == 2
    err = capsys.readouterr().err
    assert "error: TransientHTTPError: HTTP 503" in err and "Traceback" not in err
    saved = _run_log(out)
    assert saved["status"] == "failed" and saved["failed_stage"] == "crawl" and "503" in saved["error"]
    events = [json.loads(line)["event"] for line in (out / "run_events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[0] == "run_start" and "run_failed" in events


def test_robots_5xx_refuses_to_crawl(site, tmp_path, capsys):
    url, routes = site
    routes["/robots.txt"] = (500, b"oops")
    out = tmp_path / "out"
    assert _cli(url, out) == 2
    assert "robots.txt unavailable, refusing to crawl" in capsys.readouterr().err
    assert _run_log(out)["status"] == "failed"


def test_robots_group_for_the_tool_is_obeyed(site, tmp_path, capsys):
    url, routes = site
    routes["/robots.txt"] = (200, b"User-agent: playwright-scraper-demo\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    out = tmp_path / "out"
    assert _cli(url, out) == 2
    assert "robots.txt disallows" in capsys.readouterr().err


def test_success_partial_and_locked_output(site, tmp_path, capsys):
    url, routes = site
    out = tmp_path / "out"

    # 0: one category, first page only. The category has a page 2, so the run says it stopped early.
    assert _cli(url, out, "--categories", "Travel", "--max-pages", "1") == 0
    saved = _run_log(out)
    assert saved["status"] == "success" and saved["records"]["exported"] == 20
    assert saved["settings"]["workers_used"] == 1
    assert saved["categories"]["stopped_at_max_pages"] == ["Travel"]
    assert "page limit: --max-pages 1 (1 category not read to the end)" in capsys.readouterr().out
    old_xlsx = (out / "books.xlsx").read_bytes()

    # 1: page 2 of the category is missing (404, not retried) -> partial
    assert _cli(url, out, "--categories", "Travel", "--max-pages", "2") == 1
    saved = _run_log(out)
    assert saved["status"] == "partial" and saved["pages"]["failed"] == 1 and "404" in saved["errors"][0]["error"]
    old_xlsx = (out / "books.xlsx").read_bytes()

    # 2: books.xlsx open elsewhere -> one-line error, nothing replaced, run_log.json says failed
    if sys.platform != "win32":  # only Windows blocks renaming an open file; test_exporters simulates it
        pytest.skip("Windows file locking")
    capsys.readouterr()
    with open(out / "books.xlsx", "rb"):
        assert _cli(url, out, "--categories", "Travel", "--max-pages", "1") == 2
    err = capsys.readouterr().err
    assert "books.xlsx is open in another program" in err and "Traceback" not in err
    assert (out / "books.xlsx").read_bytes() == old_xlsx
    saved = _run_log(out)
    assert saved["status"] == "failed" and saved["failed_stage"] == "outputs"
    assert saved["records"]["exported"] == 0  # nothing from the failed run was delivered


def test_headless_run_downloads_html_only(site, tmp_path):
    url, routes = site
    assert _cli(url, tmp_path / "out", "--categories", "Travel", "--max-pages", "1") == 0
    # robots.txt, the home page and the listing: no stylesheet, script or image request reaches the site
    assert routes.hits == ["/robots.txt", "/", "/travel_2/index.html"]


def test_a_next_link_that_loops_ends_the_category(site, tmp_path, capsys):
    url, routes = site
    routes["/travel_2/page-2.html"] = (200, LISTING)  # page 2 links to "page-2.html" again: a loop
    assert _cli(url, tmp_path / "out", "--categories", "Travel") == 0
    saved = _run_log(tmp_path / "out")
    assert saved["pages"]["ok"] == 2 and saved["records"]["duplicates_removed"] == 20
    assert "pagination_loop" in capsys.readouterr().err


def test_ctrl_c_replaces_an_old_success_with_interrupted(site, tmp_path, monkeypatch, capsys):
    url, _ = site
    out = tmp_path / "out"
    out.mkdir()
    (out / "run_log.json").write_text('{"status": "success"}', encoding="utf-8")  # from an older run

    async def ctrl_c(context, settings, log, on_page=None):
        signal.raise_signal(signal.SIGINT)  # what Ctrl+C does: asyncio.run() cancels the main task
        await asyncio.sleep(30)

    monkeypatch.setattr(pipeline, "scrape", ctrl_c)
    assert _cli(url, out) == 130
    saved = _run_log(out)
    assert saved["status"] == "interrupted" and "Ctrl+C" in saved["error"]
    assert "interrupted" in capsys.readouterr().err


def test_repeated_category_names_are_crawled_once():
    cats = [Category(name=n, url=f"https://books.toscrape.com/{n.lower()}/index.html") for n in ("Travel", "Poetry")]
    picked = select_categories(cats, "Travel, travel,POETRY,Travel")
    assert [c.name for c in picked] == ["Travel", "Poetry"]
