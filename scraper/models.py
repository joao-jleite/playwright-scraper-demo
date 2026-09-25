"""Data models. Every scraped record goes through `Book` before it is exported."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

# The site encodes the rating as a CSS class: <p class="star-rating Three">
RATING_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_PRICE_RE = re.compile(r"(\d+(?:[.,]\d{1,2})?)")


def parse_price(value: Any) -> float:
    """'£51.77' -> 51.77. Raises ValueError when no number is present."""
    if isinstance(value, (int, float)):
        return float(value)
    match = _PRICE_RE.search(str(value or ""))
    if not match:
        raise ValueError(f"no price found in {value!r}")
    return float(match.group(1).replace(",", "."))


def parse_rating(value: Any) -> int:
    """'star-rating Three' / 'Three' / 3 -> 3. Raises ValueError otherwise."""
    if isinstance(value, int):
        return value
    for token in str(value or "").split():
        if token.lower() in RATING_WORDS:
            return RATING_WORDS[token.lower()]
    raise ValueError(f"no rating word found in {value!r}")


class Book(BaseModel):
    """One product row, validated. Invalid rows are logged and excluded, never silently kept."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1)
    price_gbp: float = Field(gt=0, lt=10_000)
    rating: int = Field(ge=1, le=5)
    availability: str = Field(min_length=1)
    in_stock: bool
    url: HttpUrl
    listing_page: int = Field(ge=1)
    scraped_at: datetime

    @field_validator("price_gbp", mode="before")
    @classmethod
    def _price(cls, v: Any) -> float:
        return round(parse_price(v), 2)

    @field_validator("rating", mode="before")
    @classmethod
    def _rating(cls, v: Any) -> int:
        return parse_rating(v)

    @classmethod
    def from_raw(cls, raw: dict[str, Any], *, category: str, listing_page: int,
                 scraped_at: datetime) -> "Book":
        """Map the dict returned by the in-page extractor to a validated Book."""
        availability = " ".join(str(raw.get("availability") or "").split())
        return cls(
            title=raw.get("title") or "",
            category=category,
            price_gbp=raw.get("price"),
            rating=raw.get("rating"),
            availability=availability,
            in_stock=availability.lower().startswith("in stock"),
            url=raw.get("url"),
            listing_page=listing_page,
            scraped_at=scraped_at,
        )

    def as_row(self) -> dict[str, Any]:
        """Flat dict used by the CSV/XLSX exporters (column order matters)."""
        return {
            "title": self.title,
            "category": self.category,
            "price_gbp": self.price_gbp,
            "rating": self.rating,
            "availability": self.availability,
            "in_stock": self.in_stock,
            "url": str(self.url),
            "scraped_at": self.scraped_at.strftime("%Y-%m-%d %H:%M:%S"),
        }


class Category(BaseModel):
    name: str
    url: str
