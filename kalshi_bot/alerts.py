"""The event feed behind the dashboard's alerts panel.

Every process that matters (the trading loop, the learning loop) appends
one JSON line per event to the same file, ``state/alerts.jsonl`` by
default, and the dashboard shows the tail. Levels: ``info`` for fills,
sales, settlements and parameter changes; ``warn`` for anything that
needs a look (a reconciliation mismatch, a shrunk size, a failed call);
``halt`` when trading has stopped.

A write failure is logged and dropped: an alert must never take the loop
down with it.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

LEVELS = ("info", "warn", "halt")
DEFAULT_PATH = Path("state/alerts.jsonl")


class AlertLog:
    """Append-only event log; ``path=None`` disables writing."""

    def __init__(self, path: str | Path | None = DEFAULT_PATH) -> None:
        self.path = Path(path) if path else None
        self.last: dict[str, Any] | None = None

    def record(
        self, level: str, source: str, text: str, now: float | None = None, **extra: Any
    ) -> dict[str, Any]:
        if level not in LEVELS:
            raise ValueError(f"alert level must be one of {LEVELS}, not {level!r}")
        row: dict[str, Any] = {
            "ts": time.time() if now is None else now,
            "level": level,
            "source": source,
            "text": text,
        }
        row.update(extra)
        self.last = row
        (log.warning if level != "info" else log.info)("%s: %s", source, text)
        if self.path is None:
            return row
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except OSError as exc:
            log.warning("alert write failed: %s", exc)
        return row


def tail(path: str | Path | None, n: int = 50, max_bytes: int = 256_000) -> list[dict[str, Any]]:
    """The last ``n`` alerts in the file, oldest first; [] when there is none."""
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    try:
        with p.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = chunk.splitlines()
    if len(chunk) >= max_bytes and lines:
        lines = lines[1:]  # the first line is probably cut
    out: list[dict[str, Any]] = []
    for line in lines[-n:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out
