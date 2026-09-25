# Playwright Web Scraper → Excel + PDF report

> **DEMO / portfolio project.** Data comes from [books.toscrape.com](https://books.toscrape.com), a public sandbox
> built for scraping practice. Its prices and ratings are randomly assigned and mean nothing in the real world.

![Demo: headed crawl, generated PDF report and Excel workbook](docs/demo.gif)

*About 20 seconds, with sped-up parts labelled. It shows a real headed crawl of 3 categories, the real log of
the full run, the generated PDF opened in Chromium, and then the generated workbook.
[MP4 version](docs/demo.mp4)*

[Leer en español](README.es.md)

---

## Problem

A retailer wants to watch a competitor's online catalog every day: what is listed, at which price, how each item
is rated and whether it is in stock. Doing that by hand means hours of copy-and-paste, missed pages and a
spreadsheet nobody trusts.

## Solution

One command crawls the whole catalog with a real browser, validates every record and delivers files a buyer or
manager can use right away:

- **`books.xlsx`**: formatted Excel tables with filters, a per-category summary and a list of price opportunities
- **`books.csv`**: flat export for BI tools or databases
- **`report.pdf`**: a 3-page report with KPIs, 2 charts and the top 10 opportunities
- **`run_log.json`**: what happened in the run (pages, retries, errors, invalid records, timings)

**Real run, full catalog** (numbers from [`examples/output/run_log.json`](examples/output/run_log.json),
collected 2026-09-25 07:30 UTC on Windows 11):

| Products | Categories | Listing pages | Failed pages | Invalid records | Crawl time |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 50 | 80 | 0 | 0 | 42.0 s |

Average price £35.07, median £35.98, average rating 2.92 / 5, **173 opportunities** (rating ≥ 4 and priced
below the median of their own category). A second full run on the same day returned the same 1,000 records
(same URLs, prices and ratings) in 35.0 s.

## Stack

| | |
|---|---|
| Language | Python 3.12 (venv) |
| Browser automation | Playwright 1.58 (async API, Chromium) |
| Validation | pydantic v2, one model per product |
| Excel | openpyxl (Excel tables, number formats, data bars, hyperlinks) |
| PDF / charts | reportlab + matplotlib |
| Tests | pytest, offline (saved HTML fixtures, no network) |

---

## Features

- **Full catalog with pagination**: discovers all 50 categories and follows every "next" link.
- **Bounded concurrency**: a fixed pool of 4 browser pages (`--concurrency`) pulls pages from a queue. Categories
  run in parallel, while pages within a category stay in order.
- **Polite by default**: 0.5 s delay plus jitter after every page, per worker. In headless mode, images, fonts
  and media are blocked, so each listing loads only its HTML, CSS and scripts. A `Crawl-delay` in robots.txt
  overrides the delay if it is longer.
- **robots.txt checked first** and enforced for every URL (see [Ethics](#ethics-and-robotstxt)).
- **Retry with exponential backoff + jitter** on timeouts, 5xx and 429. A 404 is not retried.
- **Validation before export**: every row goes through a pydantic model (price > 0, rating 1-5, valid URL,
  non-empty title). Invalid rows are left out of the exports, logged and listed in `run_log.json`.
- **Deduplication** by product URL.
- **Structured logging**: readable console lines plus a JSON Lines file (`run_events.jsonl`) with one event
  per page.
- **Exit codes for schedulers**: `0` success, `1` partial (some pages failed), `2` failed or bad arguments.
- **One extraction call per page**: a single `page.evaluate` returns all products, with no round trip per element.

### Deliverables

| File | Content |
|---|---|
| `books.xlsx` → **Data** | 1,000 rows in an Excel table (filters, banded rows, frozen header), £ format, clickable URLs |
| `books.xlsx` → **Summary** | Per category: count, average / median / min / max price, average rating, % in stock, opportunities, plus a totals row |
| `books.xlsx` → **Opportunities** | Rating ≥ 4 **and** price below the category median, sorted by rating and then by % below the median |
| `books.xlsx` → **Run Info** | Source, timestamp, duration, pages, errors, robots.txt result, tool versions |
| `report.pdf` | Cover with a **DEMO** badge and collection date/time, 8 KPIs, 2 charts (average price by category, rating distribution), top 10 opportunities, category summary |
| `books.csv` | UTF-8 with BOM, so Excel shows £ and accents correctly on double-click |
| `run_log.json` | Status, settings, robots.txt result, page and record counts, KPIs, per-category counts, errors, validation errors, one event per page |

The full sample output is in [`examples/output/`](examples/output).

### Screenshots

| Headed crawl | PDF report | Excel summary |
|---|---|---|
| [![Headed crawl](docs/screenshot-1-headed-crawl.png)](docs/screenshot-1-headed-crawl.png) | [![PDF report](docs/screenshot-2-pdf-report.png)](docs/screenshot-2-pdf-report.png) | [![Excel summary](docs/screenshot-3-excel-summary.png)](docs/screenshot-3-excel-summary.png) |

The green outlines and the dark panel in the first screenshot are a demo overlay, added only by the recording
script through the crawler's `on_page` hook. A normal run does not change the page.

---

## How to run

Requires Python 3.12+. `playwright install chromium` downloads the Chromium build that Playwright drives
(once per machine).

**Windows (PowerShell)**

```powershell
git clone https://github.com/joao-jleite/playwright-scraper-demo.git
cd playwright-scraper-demo
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium

python -m scraper                       # full catalog -> .\output
```

**Linux / macOS (bash)**

```bash
git clone https://github.com/joao-jleite/playwright-scraper-demo.git
cd playwright-scraper-demo
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium   # --with-deps installs system libraries on Linux

python -m scraper                       # full catalog -> ./output
```

### Options

```text
python -m scraper [--categories N|NAMES] [--max-pages N] [--headed] [--out DIR]
                  [--concurrency N] [--delay SECONDS] [--retries N] [--slow-mo MS]
                  [--log-level LEVEL] [--list-categories] [--version]
```

| Example | What it does |
|---|---|
| `python -m scraper` | Full catalog, headless, output in `./output` |
| `python -m scraper --categories 5 --max-pages 1` | First 5 categories, first page each (quick smoke test) |
| `python -m scraper --categories "Travel,Poetry" --headed` | Two categories by name, with a visible browser window |
| `python -m scraper --out reports/2026-09-25` | Output to a dated folder, for example from a daily scheduled task |
| `python -m scraper --list-categories` | Print the 50 category names and exit |

### Tests

```bash
pip install -r requirements-dev.txt
pytest
```

25 offline tests. Every other request is aborted, so the suite never touches the network. They cover:

- the real in-page extractor against a listing page saved from the site and a synthetic page with edge cases,
  both served through Playwright routing
- pydantic validation (valid and broken records) and price/rating parsing
- category selection, the opportunity rule and the KPIs
- the exporters end to end: CSV header and BOM, XLSX sheets, tables and formats, a 3-page PDF, and
  `run_log.json` for a partial run and for a run with no data

---

## How it works

```mermaid
flowchart LR
    A[robots.txt] --> B[Home page:<br/>50 categories]
    B --> C[Queue of<br/>listing pages]
    C --> D[4 worker pages<br/>goto + retry/backoff]
    D -- next page --> C
    D --> E[pydantic<br/>validation]
    E --> F[dedupe +<br/>analysis]
    F --> G[books.xlsx]
    F --> H[books.csv]
    F --> I[report.pdf]
    F --> J[run_log.json]
```

```text
scraper/
  cli.py          argparse CLI, exit codes
  pipeline.py     robots.txt -> categories -> crawl -> outputs; writes run_log.json
  crawler.py      async worker pool, pagination, retry with backoff, in-page extraction
  robots.py       robots.txt fetch and per-URL check (RFC 9309)
  models.py       pydantic Book model, price/rating parsers
  analysis.py     KPIs, per-category summary, opportunity rule
  excel.py        XLSX (Data, Summary, Opportunities, Run Info)
  pdf_report.py   PDF (reportlab), charts from charts.py
  log.py          console + JSON Lines structured logging
scripts/          recording of the README media (not needed to use the scraper)
tests/            pytest suite + saved HTML fixtures
examples/output/  complete output of the full run above
```

**Adapting it to another site**: the site-specific parts are the three small JavaScript selectors in
`crawler.py` (`CATEGORIES_JS`, `EXTRACT_JS`, `PAGER_JS`) and the `Book` model. Retries, concurrency,
validation, exports and reporting stay the same.

### Rebuilding the demo media

`scripts/record_demo.py` records the GIF storyboard with Playwright `record_video` (1280×800): a real headed
crawl of 3 categories, the real run logs, the PDF rendered by pdf.js and the workbook rendered from the `.xlsx`
file by `scripts/xlsx_preview.py`. `scripts/build_media.py` then speeds up the waits (with a visible badge)
and encodes `docs/demo.gif` and `docs/demo.mp4` with imageio-ffmpeg.

```bash
pip install -r requirements-dev.txt
python -m scraper --out examples/output
python scripts/record_demo.py
python scripts/build_media.py --out docs
```

---

## Ethics and robots.txt

- **Target**: books.toscrape.com is a public sandbox that exists so people can practise scraping ("We love
  being scraped!"). No login, no personal data, no paywall.
- **robots.txt**: on 2026-09-25, `https://books.toscrape.com/robots.txt` returned **HTTP 404**. Under
  [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309) §2.3.1.3 that means no crawl restrictions. The scraper
  still fetches robots.txt at the start of every run and checks every URL against it. If robots.txt is
  unreachable (5xx or network error), it refuses to crawl.
- **Load**: at most 4 pages at a time, a delay after every page, no images or fonts in headless mode, and a
  user agent that names the tool and links to this repository.
- **For real client work**: check the target's terms of service and robots.txt, prefer an official API or
  feed when there is one, respect rate limits and never collect personal data without a legal basis.

## License

[MIT](LICENSE) © João Vitor Sousa Leite
