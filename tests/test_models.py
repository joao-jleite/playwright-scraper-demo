from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from scraper.models import Book, parse_price, parse_rating

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
RAW = {"title": "A Book", "url": "https://books.toscrape.com/catalogue/a_1/index.html",
       "price": "£51.77", "availability": "\n   In stock\n ", "rating": "star-rating Four"}


@pytest.mark.parametrize("text,expected", [("£51.77", 51.77), ("Â£10.00", 10.0), ("12,50", 12.5), (7, 7.0)])
def test_parse_price(text, expected):
    assert parse_price(text) == expected


def test_parse_price_rejects_garbage():
    with pytest.raises(ValueError):
        parse_price("N/A")


@pytest.mark.parametrize("text,expected", [("star-rating One", 1), ("Five", 5), ("star-rating three", 3), (2, 2)])
def test_parse_rating(text, expected):
    assert parse_rating(text) == expected


def test_book_from_raw_normalises_fields():
    b = Book.from_raw(RAW, category="Travel", listing_page=1, scraped_at=NOW)
    assert (b.price_gbp, b.rating, b.availability, b.in_stock) == (51.77, 4, "In stock", True)
    assert b.as_row()["url"].startswith("https://")


@pytest.mark.parametrize("field,value", [("price", "free"), ("rating", "star-rating Zero"), ("title", "  "),
                                         ("url", "not-a-url")])
def test_book_rejects_invalid(field, value):
    with pytest.raises(ValidationError):
        Book.from_raw({**RAW, field: value}, category="Travel", listing_page=1, scraped_at=NOW)
