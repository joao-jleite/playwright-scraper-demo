from datetime import datetime, timezone

from scraper.analysis import dedupe, find_opportunities, kpis, summarize_by_category
from scraper.models import Book

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def book(title, category, price, rating, stock=True):
    return Book(title=title, category=category, price_gbp=price, rating=rating,
                availability="In stock" if stock else "Out of stock", in_stock=stock,
                url=f"https://example.com/{title}", listing_page=1, scraped_at=NOW)


BOOKS = [
    book("a", "X", 10, 5), book("b", "X", 20, 4), book("c", "X", 30, 5), book("d", "X", 40, 1),
    book("e", "Y", 50, 4, stock=False), book("f", "Y", 60, 2),
]


def test_opportunities_rule_rating_and_below_category_median():
    opps = find_opportunities(BOOKS)
    # X median = 25 -> a (5 stars, 10) and b (4 stars, 20) qualify; c is 5 stars but above the median.
    # Y median = 55 -> e (4 stars, 50) qualifies.
    assert [o.book.title for o in opps] == ["a", "b", "e"]
    assert round(opps[0].below_median_pct, 2) == 0.6


def test_summary_sorted_by_titles_and_counts_opportunities():
    rows = summarize_by_category(BOOKS, find_opportunities(BOOKS))
    assert [r.category for r in rows] == ["X", "Y"]
    x = rows[0]
    assert (x.titles, x.avg_price, x.min_price, x.max_price, x.opportunities) == (4, 25.0, 10, 40, 2)
    assert rows[1].in_stock_pct == 0.5


def test_kpis_and_dedupe():
    unique, dups = dedupe(BOOKS + [BOOKS[0]])
    assert (len(unique), dups) == (6, 1)
    k = kpis(unique, find_opportunities(unique))
    assert k["products"] == 6 and k["categories"] == 2 and k["opportunities"] == 3
    assert k["rating_distribution"] == {1: 1, 2: 1, 3: 0, 4: 2, 5: 2}
