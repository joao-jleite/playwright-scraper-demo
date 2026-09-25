"""PDF report (reportlab platypus): cover with KPIs, 2 charts, top opportunities, category summary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import matplotlib
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from scraper import TOOL_NAME
from scraper.analysis import CategorySummary, Opportunity

NAVY = colors.HexColor("#1F2A44")
ACCENT = colors.HexColor("#eb6834")
SOFT = colors.HexColor("#C9D3E6")
INK = colors.HexColor("#0b0b0b")
INK_2 = colors.HexColor("#52514e")
STRIPE = colors.HexColor("#F3F5F9")
HAIRLINE = colors.HexColor("#E1E0D9")
PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
FRAME_PAD = 6  # SimpleDocTemplate frames have 6 pt inner padding; the cover/footer align to it
CONTENT_X = MARGIN + FRAME_PAD
COVER_BAND_H = 92 * mm


@dataclass
class ReportInput:
    collected_at: str  # "2026-09-25 07:31 UTC"
    source: str
    robots_summary: str
    duration_s: float
    pages_ok: int
    pages_failed: int
    retries: int
    invalid_records: int
    duplicates: int
    concurrency: int  # browser pages actually used in this run (not the --concurrency ceiling)
    delay_s: float
    kpis: dict
    summary: list[CategorySummary]
    opportunities: list[Opportunity]
    chart_price: Path
    chart_rating: Path
    categories_crawled: int = 0     # how many categories this run covered...
    categories_available: int = 0   # ...out of how many the site lists
    headed: bool = False
    crawl_delay_s: float | None = None  # robots.txt Crawl-delay, when the site sets one
    max_pages: int | None = None  # --max-pages (None = every listing page of each category)
    limit_note: str | None = None  # what --max-pages left out (pipeline.page_limit_note)


def _register_fonts() -> None:
    # DejaVu ships with matplotlib and covers £, ★, accents and dashes found in titles.
    if "DejaVu" in pdfmetrics.getRegisteredFontNames():
        return
    base = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    pdfmetrics.registerFont(TTFont("DejaVu", str(base / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(base / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold",
                                  italic="DejaVu", boldItalic="DejaVu-Bold")


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("base", fontName="DejaVu", fontSize=9.5, leading=13.5, textColor=INK, alignment=TA_LEFT)
    return {
        "body": base,
        "small": ParagraphStyle("small", parent=base, fontSize=8, leading=10.5, textColor=INK_2),
        "cell": ParagraphStyle("cell", parent=base, fontSize=7.8, leading=9.6),
        "h1": ParagraphStyle("h1", parent=base, fontName="DejaVu-Bold", fontSize=15, leading=19, spaceAfter=4),
        "h2": ParagraphStyle("h2", parent=base, fontName="DejaVu-Bold", fontSize=11.5, leading=15, spaceBefore=8,
                             spaceAfter=4),
        "kpi_label": ParagraphStyle("kpi_label", parent=base, fontSize=7.8, leading=10, textColor=INK_2),
        "kpi_value": ParagraphStyle("kpi_value", parent=base, fontName="DejaVu-Bold", fontSize=17, leading=21),
    }


def _gbp(v: float) -> str:
    return f"£{v:,.2f}"


def _pct_below(p: float) -> str:
    """0.253 -> '25%'. A price a penny under the median reads '<1%' instead of a misleading '0%'."""
    return "<1%" if p < 0.01 else f"{p:.0%}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _fit_style(text: str, style: ParagraphStyle, max_width: float, min_size: float = 10) -> ParagraphStyle:
    """Same style, font size reduced until `text` fits in `max_width` (KPI tiles have a fixed width)."""
    size = style.fontSize
    while size > min_size and pdfmetrics.stringWidth(text, style.fontName, size) > max_width:
        size -= 0.5
    if size == style.fontSize:
        return style
    # same leading as the full-size style, so the tile's label stays level with its neighbors
    return ParagraphStyle(f"{style.name}_{size}", parent=style, fontSize=size, leading=style.leading)


def _footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setStrokeColor(HAIRLINE)
    canvas.line(CONTENT_X, 12 * mm, PAGE_W - CONTENT_X, 12 * mm)
    canvas.setFont("DejaVu", 7.5)
    canvas.setFillColor(INK_2)
    canvas.drawString(CONTENT_X, 8 * mm, f"DEMO report · {TOOL_NAME} · data: books.toscrape.com (public scraping sandbox)")
    canvas.drawRightString(PAGE_W - CONTENT_X, 8 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _cover(canvas, doc, data: ReportInput) -> None:
    """Navy band with the DEMO badge, title and collection timestamp (drawn directly on page 1)."""
    canvas.saveState()
    top = PAGE_H
    canvas.setFillColor(NAVY)
    canvas.rect(0, top - COVER_BAND_H, PAGE_W, COVER_BAND_H, stroke=0, fill=1)

    # DEMO pill
    x, y = CONTENT_X, top - 30 * mm
    canvas.setFillColor(ACCENT)
    canvas.roundRect(x, y, 26 * mm, 8.5 * mm, 4.25 * mm, stroke=0, fill=1)
    canvas.setFillColor(colors.white)
    canvas.setFont("DejaVu-Bold", 11)
    canvas.drawCentredString(x + 13 * mm, y + 2.7 * mm, "DEMO")

    canvas.setFont("DejaVu-Bold", 25)
    canvas.drawString(CONTENT_X, top - 45 * mm, "Competitor Catalog & Price Monitor")
    canvas.setFillColor(SOFT)
    canvas.setFont("DejaVu", 11.5)
    canvas.drawString(CONTENT_X, top - 54 * mm, "Automated snapshot of a competitor's catalog: prices, ratings, stock")
    canvas.drawString(CONTENT_X, top - 60.5 * mm, f"Source: {data.source}  (public sandbox built for scraping practice)")
    canvas.setFillColor(colors.white)
    canvas.setFont("DejaVu-Bold", 11.5)
    canvas.drawString(CONTENT_X, top - 75 * mm, f"Collected {data.collected_at}")
    canvas.setFont("DejaVu", 10)
    canvas.setFillColor(SOFT)
    canvas.drawString(CONTENT_X, top - 81.5 * mm,
                      f"Run time {data.duration_s:.1f} s · {data.pages_ok} pages crawled · "
                      f"{data.pages_failed} failed · {data.invalid_records} invalid records")
    canvas.restoreState()
    _footer(canvas, doc)


def _kpi_grid(data: ReportInput, st: dict[str, ParagraphStyle], width: float) -> Table:
    k = data.kpis
    tiles = [
        ("Products", f"{k['products']:,}"),
        ("Categories", f"{k['categories']}"),
        ("Average price", _gbp(k["avg_price"])),
        ("Median price", _gbp(k["median_price"])),
        ("Average rating", f"{k['avg_rating']:.2f} / 5"),
        ("In stock", f"{k['in_stock_pct']:.0%}"),
        ("Opportunities", f"{k['opportunities']}"),
        ("Price range", f"{_gbp(k['min_price'])}–{_gbp(k['max_price'])}"),  # exact, never rounded
    ]
    col_w = width / 4
    text_w = col_w - 9 - 6 - 2  # tile width minus left/right padding and a small safety margin
    cells = [[Paragraph(label, st["kpi_label"]), Paragraph(value, _fit_style(value, st["kpi_value"], text_w))]
             for label, value in tiles]
    grid = [cells[0:4], cells[4:8]]
    t = Table(grid, colWidths=[col_w] * 4, rowHeights=[20 * mm, 20 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), STRIPE),
        ("LINEAFTER", (0, 0), (-2, -1), 2, colors.white),   # surface gap between tiles
        ("LINEBELOW", (0, 0), (-1, 0), 2, colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def _table(header: list[str], rows: list[list], col_w: list[float], align_right_from: int) -> Table:
    t = Table([header] + rows, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "DejaVu-Bold", 7.8),
        ("FONT", (0, 1), (-1, -1), "DejaVu", 7.8),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, STRIPE]),
        ("ALIGN", (align_right_from, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.2),
        ("LINEBELOW", (0, -1), (-1, -1), 0.6, HAIRLINE),
    ]))
    return t


def _market_sentence(summary: list[CategorySummary], shown: int, overall_avg: float) -> str:
    """Page 2 lead sentence. Wording follows the real number of categories (partial runs included)."""
    top = summary[:shown]
    if not top:
        return ""
    if len(top) == 1:
        c = top[0]
        return (f"This run covers one category, <b>{escape(c.category)}</b>, with an average price of "
                f"{_gbp(c.avg_price)}.")
    scope = (f"Among the {len(top)} largest of {len(summary)} categories" if len(summary) > len(top)
             else f"Among the {len(top)} categories in this run")
    priciest = max(top, key=lambda s: s.avg_price)
    cheapest = min(top, key=lambda s: s.avg_price)
    return (f"{scope}, <b>{escape(priciest.category)}</b> has the highest average price "
            f"({_gbp(priciest.avg_price)}) and <b>{escape(cheapest.category)}</b> the lowest "
            f"({_gbp(cheapest.avg_price)}). Overall average: {_gbp(overall_avg)}.")


def _opportunities_intro(total: int, shown: int) -> str:
    rule = "Rule: rating ≥ 4 stars and price below the median of its own category. "
    if total == 0:
        return rule + "No title matches it in this run."
    order = "sorted by rating, then by how far they sit under the median."
    if total > shown:
        return rule + f"{total} titles match; the top {shown} are listed below, {order}"
    return rule + f"{_plural(total, 'title')} match{'es' if total == 1 else ''}, all listed below, {order}"


def write_pdf(path: Path, data: ReportInput, top_categories: int = 12, top_opportunities: int = 10,
              summary_rows: int = 15) -> int:
    """Build the report. Returns the number of pages actually written."""
    _register_fonts()
    st = _styles()
    width = PAGE_W - 2 * CONTENT_X  # usable frame width
    k = data.kpis
    story: list = []
    # "all 50" for a full crawl, "3 of 50" for a partial one (--categories)
    crawled = data.categories_crawled or k["categories"]
    scope = (f"all {crawled}" if crawled >= data.categories_available
             else f"{crawled} of {data.categories_available}")
    mode = "headed" if data.headed else "headless"
    pages_used = "1 browser page" if data.concurrency == 1 else f"{data.concurrency} concurrent browser pages"
    robots_text = data.robots_summary[:1].upper() + data.robots_summary[1:]
    if data.max_pages is None:
        walk = f"walks {scope} category listings with pagination"
    else:  # --max-pages: what was left out is spelled out in the "Page limit" paragraph
        first = "listing page" if data.max_pages == 1 else f"{data.max_pages} listing pages"
        cats = (f"all {crawled} categories" if crawled >= data.categories_available
                else f"{crawled} categories (out of {data.categories_available} on the site)")
        walk = f"reads the first {first} of each of {cats}"
    robots_delay = (f" The site's robots.txt Crawl-delay of {data.crawl_delay_s:g} s is enforced across all "
                    "pages." if data.crawl_delay_s else "")

    # ------------------------------------------------------------ page 1
    story.append(Spacer(1, COVER_BAND_H - MARGIN + 8 * mm))
    story.append(Paragraph("Key numbers", st["h2"]))
    story.append(_kpi_grid(data, st, width))
    story.append(Spacer(1, 7 * mm))
    story.append(Paragraph("About this run", st["h2"]))
    about = [
        f"<b>Scenario.</b> A retailer wants a daily view of a competitor's catalog: what is listed, at what "
        f"price, how it is rated and whether it is in stock. This report is generated automatically from that crawl.",
        f"<b>Method.</b> Playwright (Chromium, {mode}) {walk}, "
        f"using {pages_used}, a {data.delay_s:.1f} s polite delay with "
        f"jitter and retries with exponential backoff.{robots_delay} Every record is validated with pydantic "
        f"before export.",
        *([f"<b>Page limit.</b> {escape(data.limit_note)}"] if data.limit_note else []),
        f"<b>robots.txt.</b> {escape(robots_text)}.",
        f"<b>Quality.</b> {data.pages_ok} listing pages OK, {data.pages_failed} failed, {data.retries} retries, "
        f"{data.invalid_records} invalid records, {data.duplicates} duplicates removed.",
        "<b>Deliverables.</b> books.xlsx (Data, Summary, Opportunities, Run Info), books.csv, this PDF and "
        "run_log.json.",
        "<b>DEMO notice.</b> books.toscrape.com is a sandbox: its prices and ratings are randomly assigned "
        "and have no real meaning. The numbers here only show what the pipeline produces.",
    ]
    for p in about:
        story.append(Paragraph(p, st["body"]))
        story.append(Spacer(1, 2.2 * mm))
    story.append(PageBreak())

    # ------------------------------------------------------------ page 2
    story.append(Paragraph("Market overview", st["h1"]))
    lead = _market_sentence(data.summary, top_categories, k["avg_price"])
    if lead:
        story.append(Paragraph(lead, st["body"]))
    story.append(Spacer(1, 3 * mm))
    story.append(Image(str(data.chart_price), width=width, height=width * 4.1 / 7.2))
    story.append(Spacer(1, 5 * mm))
    dist = k["rating_distribution"]
    four_plus = (dist.get(4, 0) + dist.get(5, 0)) / max(k["products"], 1)
    story.append(Paragraph(f"{four_plus:.0%} of the titles are rated 4 or 5 stars; the average rating is "
                           f"{k['avg_rating']:.2f}.", st["body"]))
    story.append(Spacer(1, 3 * mm))
    story.append(Image(str(data.chart_rating), width=width, height=width * 3.0 / 7.2))
    story.append(PageBreak())

    # ------------------------------------------------------------ page 3
    opps = data.opportunities[:top_opportunities]
    story.append(Paragraph(f"Top {len(opps)} opportunities" if opps else "Opportunities", st["h1"]))
    story.append(Paragraph(_opportunities_intro(len(data.opportunities), len(opps)), st["small"]))
    story.append(Spacer(1, 3 * mm))
    if opps:
        rows = []
        for i, o in enumerate(opps, start=1):
            rows.append([str(i), Paragraph(escape(o.book.title), st["cell"]),
                         Paragraph(escape(o.book.category), st["cell"]), _gbp(o.book.price_gbp),
                         _gbp(o.category_median), _pct_below(o.below_median_pct), "★" * o.book.rating])
        story.append(_table(["#", "Title", "Category", "Price", "Category\nmedian", "Below\nmedian", "Rating"],
                            rows, [7 * mm, 58 * mm, 28 * mm, 16 * mm, 20 * mm, 20 * mm, 20 * mm],
                            align_right_from=3))

    story.append(Spacer(1, 8 * mm))
    cats = data.summary[:summary_rows]
    cat_rows = [[Paragraph(escape(s.category), st["cell"]), str(s.titles), _gbp(s.avg_price), _gbp(s.median_price),
                 _gbp(s.min_price), _gbp(s.max_price), f"{s.avg_rating:.2f}", str(s.opportunities)]
                for s in cats]
    truncated = len(data.summary) > len(cats)
    if truncated:
        heading = f"Category summary ({len(cats)} largest of {len(data.summary)} categories)"
    else:
        heading = f"Category summary ({len(cats)} categories)" if len(cats) > 1 else "Category summary"
    block = [Paragraph(heading, st["h2"])]
    if truncated:
        block.append(Paragraph("Full list of categories in books.xlsx → Summary.", st["small"]))
    if data.max_pages is not None:
        block.append(Paragraph(f"Titles = products on the listing pages this run read (--max-pages "
                               f"{data.max_pages}), not necessarily the whole category.", st["small"]))
    block += [
        Spacer(1, 2 * mm),
        _table(["Category", "Titles", "Avg price", "Median", "Min", "Max", "Avg rating", "Opport."], cat_rows,
               [40 * mm, 14 * mm, 19 * mm, 19 * mm, 17 * mm, 17 * mm, 22 * mm, 21 * mm], align_right_from=1),
    ]
    story.append(KeepTogether(block))

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN,
                            bottomMargin=18 * mm, title="Competitor Catalog & Price Monitor (DEMO)",
                            author=TOOL_NAME, creator=TOOL_NAME, subject="Web scraping demo report")
    doc.build(story, onFirstPage=lambda c, d: _cover(c, d, data), onLaterPages=_footer)
    return doc.page  # reportlab leaves the last page number here after build()
