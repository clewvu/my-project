"""Scheduled-event blackout.

On a 15-minute horizon, politics and macro reach the tape as scheduled releases
(CPI, jobs, FOMC, Treasury auctions, speeches) and the informed flow around
them. A fair-value model built on the last half hour of volatility is the
liquidity those traders take. The defensible edge is to stand aside: no new
entries from ``before`` minutes before a listed event until ``after`` minutes
after it. Events live in ``events.yaml`` (UTC):

    events:
      - {at: "2026-11-13T13:30:00Z", name: "US CPI", before: 10, after: 15}
      - {at: "2026-11-12T19:00:00Z", name: "FOMC statement", before: 15, after: 30}
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Event:
    at: float  # unix seconds
    name: str
    before: float  # seconds
    after: float  # seconds

    def covers(self, now: float) -> bool:
        return self.at - self.before <= now <= self.at + self.after


class EventBlackout:
    def __init__(self, events: list[Event]) -> None:
        self.events = sorted(events, key=lambda e: e.at)

    @classmethod
    def load(cls, path: str | Path = "events.yaml") -> EventBlackout:
        p = Path(path).expanduser()
        if not p.exists():
            return cls([])
        data = yaml.safe_load(p.read_text()) or {}
        events = []
        for row in data.get("events", []) or []:
            at = row["at"]
            if isinstance(at, datetime):
                dt = at if at.tzinfo else at.replace(tzinfo=UTC)
            else:
                text = str(at)
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                dt = datetime.fromisoformat(text)
                dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
            events.append(
                Event(
                    at=dt.timestamp(),
                    name=str(row.get("name", "event")),
                    before=float(row.get("before", 10)) * 60,
                    after=float(row.get("after", 15)) * 60,
                )
            )
        return cls(events)

    def reason(self, now: float) -> str | None:
        for e in self.events:
            if e.covers(now):
                return e.name
        return None

    def upcoming(self, now: float, within: float = 86400) -> list[Event]:
        return [e for e in self.events if now <= e.at <= now + within]
