"""Structured logging: human-readable console + JSON Lines file.

Usage:
    log = setup_logging(out_dir / "run_events.jsonl")
    event(log, "page_ok", category="Travel", page=1, items=11)

All timestamps are UTC (console lines end the time with "Z"), like the PDF, the workbook and
run_log.json.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

LOGGER_NAME = "scraper"


def _fmt_value(v: Any) -> str:
    if isinstance(v, str):
        return f'"{v}"' if (" " in v or not v) else v
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def format_console(ts: datetime, level: str, name: str, fields: dict[str, Any]) -> str:
    """`07:12:28Z INFO  page_ok          category=Travel page=1/1 items=11` (time in UTC)."""
    kv = " ".join(f"{k}={_fmt_value(v)}" for k, v in fields.items())
    utc = ts.astimezone(timezone.utc)
    return f"{utc.strftime('%H:%M:%S')}Z {level:<5} {name:<16} {kv}".rstrip()


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return format_console(datetime.fromtimestamp(record.created, tz=timezone.utc), record.levelname,
                              record.getMessage(), getattr(record, "fields", {}) or {})


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


class DeferredJsonlHandler(logging.Handler):
    """JSON Lines handler that keeps events in memory until `open()` gives it a file.

    The CLI only knows the run is valid (the --categories names exist) after the home page was read.
    Deferring the file means a run rejected for a bad argument leaves no output folder behind, while
    a valid run still gets every event from the very first one.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter())
        self._buffer: list[str] = []
        self._fh: TextIO | None = None
        self.path: Path | None = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            if self._fh is None:
                self._buffer.append(line)
            else:
                self._fh.write(line + "\n")
                self._fh.flush()
        except Exception:
            self.handleError(record)

    def open(self, path: Path) -> None:
        with self.lock:
            if self._fh is not None:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("w", encoding="utf-8")
            self.path = path
            for line in self._buffer:
                self._fh.write(line + "\n")
            self._buffer.clear()
            self._fh.flush()

    def close(self) -> None:
        with self.lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
        super().close()


def setup_logging(jsonl_path: Path | None = None, level: str = "INFO", *, deferred: bool = False) -> logging.Logger:
    """Console handler + JSON Lines file. `deferred=True`: the file is opened later by open_event_file()."""
    log = logging.getLogger(LOGGER_NAME)
    log.setLevel(level.upper())
    log.propagate = False
    for h in list(log.handlers):  # idempotent when called twice (tests, demo recorder)
        log.removeHandler(h)
        h.close()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(ConsoleFormatter())
    log.addHandler(console)

    if deferred:
        log.addHandler(DeferredJsonlHandler())
    elif jsonl_path is not None:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(jsonl_path, mode="w", encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        log.addHandler(fh)
    return log


def open_event_file(log: logging.Logger, path: Path) -> None:
    """Give the deferred JSON Lines handler its file (no-op when logging was set up with a path)."""
    for h in log.handlers:
        if isinstance(h, DeferredJsonlHandler):
            h.open(path)


def event(log: logging.Logger, name: str, level: int = logging.INFO, **fields: Any) -> None:
    log.log(level, name, extra={"fields": fields})
