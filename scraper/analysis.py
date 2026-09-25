"""Business view of the scraped catalog: KPIs, per-category summary and price opportunities.

Plain Python (statistics module) on purpose: ~1,000 rows do not need pandas.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import mean, median

from scraper.models import Book

OPPORTUNITY_MIN_RATING = 4


@dataclass(frozen=True)
class CategorySummary:
    category: str
    titles: int
    avg_price: float
    min_price: float
    max_price: float
    median_price: float
    avg_rating: float
    in_stock_pct: float
    opportunities: int


@dataclass(frozen=True)
class Opportunity:
    book: Book
    category_median: float

    @property
    def below_median_pct(self) -> float:
        """How much cheaper than the category median (0.25 = 25% below)."""
        return (self.category_median - self.book.price_gbp) / self.category_median


def dedupe(books: list[Book]) -> tuple[list[Book], int]:
    """Drop repeated product URLs (keeps first). Returns (unique_books, duplicates_removed)."""
    seen: set[str] = set()
    unique: list[Book] = []
    for b in books:
        key = str(b.url)
        if key not in seen:
            seen.add(key)
            unique.append(b)
    return unique, len(books) - len(unique)


def _by_category(books: list[Book]) -> dict[str, list[Book]]:
    groups: dict[str, list[Book]] = defaultdict(list)
    for b in books:
        groups[b.category].append(b)
    return groups


def find_opportunities(books: list[Book], min_rating: int = OPPORTUNITY_MIN_RATING) -> list[Opportunity]:
    """Well-rated items priced below their own category median.

    Sorted by rating (desc), then by how far below the median they are (desc).
    """
    opps: list[Opportunity] = []
    for items in _by_category(books).values():
        cat_median = median(b.price_gbp for b in items)
        for b in items:
            if b.rating >= min_rating and b.price_gbp < cat_median:
                opps.append(Opportunity(b, round(cat_median, 2)))
    opps.sort(key=lambda o: (-o.book.rating, -o.below_median_pct, o.book.title))
    return opps


def summarize_by_category(books: list[Book], opportunities: list[Opportunity]) -> list[CategorySummary]:
    opp_count: dict[str, int] = defaultdict(int)
    for o in opportunities:
        opp_count[o.book.category] += 1

    rows = []
    for cat, items in _by_category(books).items():
        prices = [b.price_gbp for b in items]
        rows.append(CategorySummary(
            category=cat,
            titles=len(items),
            avg_price=round(mean(prices), 2),
            min_price=min(prices),
            max_price=max(prices),
            median_price=round(median(prices), 2),
            avg_rating=round(mean(b.rating for b in items), 2),
            in_stock_pct=round(sum(b.in_stock for b in items) / len(items), 4),
            opportunities=opp_count[cat],
        ))
    rows.sort(key=lambda r: (-r.titles, r.category))
    return rows


def kpis(books: list[Book], opportunities: list[Opportunity]) -> dict[str, float | int]:
    if not books:
        return {"products": 0, "categories": 0, "avg_price": 0, "median_price": 0,
                "min_price": 0, "max_price": 0, "avg_rating": 0, "in_stock_pct": 0,
                "opportunities": 0, "rating_distribution": {}}
    prices = [b.price_gbp for b in books]
    dist = {r: sum(1 for b in books if b.rating == r) for r in range(1, 6)}
    return {
        "products": len(books),
        "categories": len({b.category for b in books}),
        "avg_price": round(mean(prices), 2),
        "median_price": round(median(prices), 2),
        "min_price": min(prices),
        "max_price": max(prices),
        "avg_rating": round(mean(b.rating for b in books), 2),
        "in_stock_pct": round(sum(b.in_stock for b in books) / len(books), 4),
        "opportunities": len(opportunities),
        "rating_distribution": dist,
    }
