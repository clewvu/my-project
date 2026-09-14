"""A tiny scheduled-event calendar for the event-risk filter.

Scheduled macro events (FOMC, CPI, jobs, EIA oil inventories, OPEC) cause gaps and
volatility spikes that break the model's driftless-GBM assumption. We cannot
predict them, but we can stand aside: the loop reads a calendar file and pauses
*new entries* during a blackout window around each event (open positions are still
managed and settle normally).

Calendar file: JSON list of events, each with a time and an optional label and
per-event pads (seconds before/after). Times may be unix seconds or ISO-8601:

    [
      {"time": "2026-09-17T18:00:00Z", "label": "FOMC", "before_s": 1800, "after_s": 1800},
      {"time": "2026-09-10T12:30:00Z", "label": "CPI"}
    ]

Missing pads fall back to the calendar's defaults. The file is re-read when it
changes, so it can be updated without restarting the loop.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


class EventCalendar:
    def __init__(
        self,
        path: str | Path | None,
        default_before_s: float = 900.0,
        default_after_s: float = 900.0,
    ) -> None:
        self.path = Path(path) if path else None
        self.default_before_s = default_before_s
        self.default_after_s = default_after_s
        self._mtime: float | None = None
        self._windows: list[tuple[float, float, str]] = []  # (start, end, label)

    @staticmethod
    def _to_ts(value: object) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            s = value.strip().replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(s).timestamp()
            except ValueError:
                return None
        return None

    def _reload(self) -> None:
        if self.path is None or not self.path.exists():
            self._windows = []
            return
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        windows: list[tuple[float, float, str]] = []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("event calendar unreadable (%s); ignoring", exc)
            self._windows = []
            return
        for item in data or []:
            t = self._to_ts(item.get("time"))
            if t is None:
                continue
            before = float(item.get("before_s", self.default_before_s))
            after = float(item.get("after_s", self.default_after_s))
            windows.append((t - before, t + after, str(item.get("label", "event"))))
        self._windows = windows

    def active(self, now: float) -> str | None:
        """Label of an event whose blackout window covers ``now``, or None."""
        self._reload()
        for start, end, label in self._windows:
            if start <= now <= end:
                return label
        return None
