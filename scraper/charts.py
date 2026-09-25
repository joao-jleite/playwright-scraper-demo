"""Static charts (matplotlib, Agg backend) embedded in the PDF report."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: no GUI toolkit needed
import matplotlib.pyplot as plt  # noqa: E402

from scraper.analysis import CategorySummary  # noqa: E402

# Single-series charts -> one hue, text in neutral ink, recessive hairline grid.
SERIES = "#2a78d6"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
DPI = 200


def _style(ax) -> None:
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.tick_params(colors=INK_2, labelsize=8.5, length=0)
    ax.set_axisbelow(True)


def price_by_category(summary: list[CategorySummary], catalog_avg: float, path: Path, top_n: int = 12) -> Path:
    """Horizontal bars: average price of the N largest categories, sorted by price."""
    rows = sorted(summary[:top_n], key=lambda s: s.avg_price)  # summary is already sorted by titles
    labels = [f"{s.category} ({s.titles})" for s in rows]
    values = [s.avg_price for s in rows]

    fig, ax = plt.subplots(figsize=(7.2, 4.1), dpi=DPI)
    ax.barh(labels, values, height=0.55, color=SERIES)
    _style(ax)
    ax.spines["bottom"].set_visible(False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.xaxis.set_major_formatter(lambda x, _: f"£{x:.0f}")
    for y, v in enumerate(values):  # value at the bar tip, in ink (never the series color)
        ax.text(v + max(values) * 0.01, y, f"£{v:.2f}", va="center", fontsize=8, color=INK)
    ax.axvline(catalog_avg, color=MUTED, linewidth=1)
    ax.text(catalog_avg, len(values) - 0.35, f" catalog avg £{catalog_avg:.2f}", color=INK_2, fontsize=7.5,
            va="bottom")
    ax.set_xlim(0, max(values) * 1.16)
    ax.set_title(f"Average price, {len(rows)} largest categories (title count in brackets)",
                 loc="left", fontsize=10, color=INK, pad=10)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return path


def rating_distribution(distribution: dict[int, int], path: Path) -> Path:
    """Columns: how many titles have 1..5 stars."""
    stars = [1, 2, 3, 4, 5]
    counts = [distribution.get(s, 0) for s in stars]
    total = sum(counts) or 1

    fig, ax = plt.subplots(figsize=(7.2, 3.0), dpi=DPI)
    ax.bar([f"{s} ★" for s in stars], counts, width=0.45, color=SERIES)
    _style(ax)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    for x, c in enumerate(counts):
        ax.text(x, c + max(counts) * 0.02, f"{c}  ({c / total:.0%})", ha="center", va="bottom", fontsize=8,
                color=INK)
    ax.set_ylim(0, max(counts) * 1.18)
    ax.set_title("Titles by star rating", loc="left", fontsize=10, color=INK, pad=10)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return path
