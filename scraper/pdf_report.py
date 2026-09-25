"""PDF report (reportlab platypus): cover with KPIs, 2 charts, top-10 table, category summary."""

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
    concurrency: int
    delay_s: float
    kpis: dict
    summary: list[CategorySummary]
    opportunities: list[Opportunity]
    chart_price: Path
    chart_rating: Path
    categories_crawled: int = 0     # how many categories this run covered...
    categories_available: int = 0   # ...out of how many the site lists
    headed: bool = False


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
        ("Price range", f"£{k['min_price']:.0f}–£{k['max_price']:.0f}"),
    ]
    cells = [[Paragraph(label, st["kpi_label"]), Paragraph(value, st["kpi_value"])] for label, value in tiles]
    grid = [cells[0:4], cells[4:8]]
    col_w = width / 4
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


def write_pdf(path: Path, data: ReportInput) -> Path:
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

    # ------------------------------------------------------------ page 1
    story.append(Spacer(1, COVER_BAND_H - MARGIN + 8 * mm))
    story.append(Paragraph("Key numbers", st["h2"]))
    story.append(_kpi_grid(data, st, width))
    story.append(Spacer(1, 7 * mm))
    story.append(Paragraph("About this run", st["h2"]))
    about = [
        f"<b>Scenario.</b> A retailer wants a daily view of a competitor's catalog: what is listed, at which "
        f"price, how it is rated and whether it is in stock. This report is generated automatically from that crawl.",
        f"<b>Method.</b> Playwright (Chromium, {mode}) walks {scope} category listings with "
        f"pagination, using {data.concurrency} concurrent pages, a {data.delay_s:.1f} s polite delay with jitter "
        f"and retries with exponential backoff. Every record is validated with pydantic before export.",
        f"<b>robots.txt.</b> {escape(data.robots_summary)}.",
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
    top = data.summary[:12]
    if top:
        priciest = max(top, key=lambda s: s.avg_price)
        cheapest = min(top, key=lambda s: s.avg_price)
        story.append(Paragraph(
            f"Among the 12 largest categories, <b>{escape(priciest.category)}</b> has the highest average price "
            f"({_gbp(priciest.avg_price)}) and <b>{escape(cheapest.category)}</b> the lowest "
            f"({_gbp(cheapest.avg_price)}). Catalog average: {_gbp(k['avg_price'])}.", st["body"]))
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
    story.append(Paragraph("Top 10 opportunities", st["h1"]))
    story.append(Paragraph(
        f"Rule: rating ≥ 4 stars and price below the median of its own category. {k['opportunities']} titles match; "
        "the ten below are sorted by rating, then by how far they sit under the median.", st["small"]))
    story.append(Spacer(1, 3 * mm))
    rows = []
    for i, o in enumerate(data.opportunities[:10], start=1):
        rows.append([str(i), Paragraph(escape(o.book.title), st["cell"]), Paragraph(escape(o.book.category), st["cell"]),
                     _gbp(o.book.price_gbp), _gbp(o.category_median), f"−{o.below_median_pct:.0%}",
                     "★" * o.book.rating])
    story.append(_table(["#", "Title", "Category", "Price", "Cat. median", "vs median", "Rating"], rows,
                        [7 * mm, 61 * mm, 28 * mm, 16 * mm, 20 * mm, 17 * mm, 20 * mm], align_right_from=3))

    story.append(Spacer(1, 8 * mm))
    cat_rows = [[Paragraph(escape(s.category), st["cell"]), str(s.titles), _gbp(s.avg_price), _gbp(s.median_price),
                 _gbp(s.min_price), _gbp(s.max_price), f"{s.avg_rating:.2f}", str(s.opportunities)]
                for s in data.summary[:15]]
    story.append(KeepTogether([
        Paragraph("Category summary (15 largest categories)", st["h2"]),
        Paragraph("Full list of categories in books.xlsx → Summary.", st["small"]),
        Spacer(1, 2 * mm),
        _table(["Category", "Titles", "Avg price", "Median", "Min", "Max", "Avg rating", "Opport."], cat_rows,
               [40 * mm, 14 * mm, 19 * mm, 19 * mm, 17 * mm, 17 * mm, 22 * mm, 21 * mm], align_right_from=1),
    ]))

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN,
                            bottomMargin=18 * mm, title="Competitor Catalog & Price Monitor (DEMO)",
                            author=TOOL_NAME, creator=TOOL_NAME, subject="Web scraping demo report")
    doc.build(story, onFirstPage=lambda c, d: _cover(c, d, data), onLaterPages=_footer)
    return path
