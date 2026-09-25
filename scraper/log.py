"""Structured logging: human-readable console + JSON Lines file.

Usage:
    log = setup_logging(out_dir / "run_events.jsonl")
    event(log, "page_ok", category="Travel", page=1, items=11)
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER_NAME = "scraper"


def _fmt_value(v: Any) -> str:
    if isinstance(v, str):
        return f'"{v}"' if (" " in v or not v) else v
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def format_console(ts: datetime, level: str, name: str, fields: dict[str, Any]) -> str:
    """`07:12:28 INFO  page_ok          category=Travel page=1/1 items=11`"""
    kv = " ".join(f"{k}={_fmt_value(v)}" for k, v in fields.items())
    return f"{ts.strftime('%H:%M:%S')} {level:<5} {name:<16} {kv}".rstrip()


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return format_console(datetime.fromtimestamp(record.created), record.levelname, record.getMessage(),
                              getattr(record, "fields", {}) or {})


class JsonFormatter(logging.Formatter):
    """One JSON object per line, UTC timestamps, event name + fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
            **(getattr(record, "fields", {}) or {}),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(jsonl_path: Path | None = None, level: str = "INFO") -> logging.Logger:
    log = logging.getLogger(LOGGER_NAME)
    log.setLevel(level.upper())
    log.propagate = False
    for h in list(log.handlers):  # idempotent when called twice (tests, demo recorder)
        log.removeHandler(h)
        h.close()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(ConsoleFormatter())
    log.addHandler(console)

    if jsonl_path is not None:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(jsonl_path, mode="w", encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        log.addHandler(fh)
    return log


def event(log: logging.Logger, name: str, level: int = logging.INFO, **fields: Any) -> None:
    log.log(level, name, extra={"fields": fields})
