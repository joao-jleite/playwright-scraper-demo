"""Runs the real in-page extractor against saved HTML (no network).

Two fixtures:
- listing.html       small synthetic page with edge cases (bad price, out of stock, "&" in a title)
- mystery_page1.html a listing page saved from books.toscrape.com, so the selectors are checked
                     against the site's real markup
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from playwright.async_api import Route, async_playwright
from pydantic import ValidationError

from scraper.crawler import CATEGORIES_JS, EXTRACT_JS, PAGER_JS, _parse_total_pages
from scraper.models import Book, Category
from scraper.pipeline import CategorySelectionError, select_categories

FIXTURES = Path(__file__).parent / "fixtures"
SITE = "https://books.toscrape.com/catalogue/category/books/"


def _extract(fixture: str, url: str):
    """Serve `fixture` at `url` (so relative links resolve like on the site) and run the extractors.

    Every other request (CSS, JS, images) is aborted: the test never touches the network.
    """
    body = (FIXTURES / fixture).read_text(encoding="utf-8")

    async def handler(route: Route) -> None:
        if route.request.url == url:
            await route.fulfill(body=body, content_type="text/html; charset=utf-8")
        else:
            await route.abort()

    async def go():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page()
            await page.route("**/*", handler)
            await page.goto(url, wait_until="domcontentloaded")
            out = (await page.evaluate(EXTRACT_JS), await page.evaluate(PAGER_JS),
                   await page.evaluate(CATEGORIES_JS))
            await browser.close()
            return out
    return asyncio.run(go())


def _validate(items, category):
    now = datetime.now(timezone.utc)
    ok, bad = [], 0
    for raw in items:
        try:
            ok.append(Book.from_raw(raw, category=category, listing_page=1, scraped_at=now))
        except ValidationError:
            bad += 1
    return ok, bad


def test_extractor_and_validation_on_synthetic_edge_cases():
    base = SITE + "alpha_2/"
    items, pager, cats = _extract("listing.html", base + "index.html")
    assert len(items) == 3
    assert items[0]["url"] == "https://books.toscrape.com/catalogue/first-book_1/index.html"
    assert pager == {"current": "Page 1 of 2", "next": base + "page-2.html"}
    assert _parse_total_pages(pager["current"]) == 2
    assert [c["name"] for c in cats] == ["Alpha", "Beta"]

    ok, bad = _validate(items, "Alpha")
    assert bad == 1  # the "N/A" price is rejected, never exported
    assert ok[0].title == "First Book: A Test" and ok[0].rating == 3 and ok[0].in_stock
    assert ok[1].title == "Second & Last" and not ok[1].in_stock and ok[1].rating == 5


def test_extractor_on_saved_real_page():
    base = SITE + "mystery_3/"
    items, pager, cats = _extract("mystery_page1.html", base + "index.html")

    # Listing: 20 products per page, "Page 1 of 2" with a next link
    assert len(items) == 20
    assert pager == {"current": "Page 1 of 2", "next": base + "page-2.html"}
    assert _parse_total_pages(pager["current"]) == 2

    # Sidebar: all 50 categories with absolute URLs
    assert len(cats) == 50
    assert cats[0] == {"name": "Travel", "url": SITE + "travel_2/index.html"}

    ok, bad = _validate(items, "Mystery")
    assert bad == 0 and len(ok) == 20
    first = ok[0]
    # Full title comes from the link's title attribute (the visible text is truncated on the site)
    assert first.title == "Sharp Objects"
    assert str(first.url) == "https://books.toscrape.com/catalogue/sharp-objects_997/index.html"
    assert (first.price_gbp, first.rating, first.in_stock) == (47.82, 4, True)
    assert all(1 <= b.rating <= 5 and 0 < b.price_gbp < 100 for b in ok)


def test_select_categories():
    cats = [Category(name=n, url=f"https://x/{n}") for n in ["Travel", "Mystery", "Poetry"]]
    assert [c.name for c in select_categories(cats, None)] == ["Travel", "Mystery", "Poetry"]
    assert [c.name for c in select_categories(cats, "2")] == ["Travel", "Mystery"]
    assert [c.name for c in select_categories(cats, "poetry, TRAVEL")] == ["Poetry", "Travel"]
    with pytest.raises(CategorySelectionError, match="Cooking"):
        select_categories(cats, "Cooking")
