"""Data models. Every scraped record goes through `Book` before it is exported."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

# The site encodes the rating as a CSS class: <p class="star-rating Three">
RATING_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
# First number in the text, separators included: "£1,234.56" -> "1,234.56". A space (also the
# no-break spaces used by some locales) counts only when a group of exactly three digits follows it.
_NUMBER_RE = re.compile(r"\d(?:[\d.,]|[ \u00a0\u202f](?=\d{3}(?!\d)))*")
_SPACES = str.maketrans("", "", " \u00a0\u202f")


def parse_price(value: Any) -> float:
    """'£51.77' -> 51.77, '£1,234.56' -> 1234.56, '1.234,56 €' -> 1234.56, '12,50' -> 12.5.

    Raises ValueError when no number is present or the separators are ambiguous.
    """
    if isinstance(value, bool):
        raise ValueError(f"not a price: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER_RE.search(str(value or ""))
    if not match:
        raise ValueError(f"no price found in {value!r}")
    text = match.group(0).translate(_SPACES).rstrip(".,")  # "£5." -> "5"
    dot, comma = text.rfind("."), text.rfind(",")
    if dot >= 0 and comma >= 0:
        # Both present: the last one is the decimal separator, the other groups thousands.
        decimal, thousands = (".", ",") if dot > comma else (",", ".")
        return float(text.replace(thousands, "").replace(decimal, "."))
    if dot < 0 and comma < 0:
        return float(text)
    sep = "." if dot >= 0 else ","
    head, *groups = text.split(sep)
    if len(groups) > 1:  # "1,234,567" / "1.234.567": only valid as thousands groups
        if all(len(g) == 3 for g in groups):
            return float(head + "".join(groups))
        raise ValueError(f"ambiguous price {value!r}")
    # One separator: three digits after it and a non-zero head ("1,234", "12.500") means thousands;
    # otherwise it is the decimal separator ("51.77", "12,50", "0.500").
    if len(groups[0]) == 3 and head != "0":
        return float(head + groups[0])
    return float(f"{head}.{groups[0]}")


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
