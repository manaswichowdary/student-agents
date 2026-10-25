"""Small shared helpers: a controllable clock, Phoenix time and canonical JSON."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

PHX = timezone(timedelta(hours=-7), "America/Phoenix")  # Arizona has no daylight saving time
DAY = 86_400
WEEK = 7 * DAY


class Clock:
    """Real time by default. Tests and the demo can freeze or advance it."""

    def __init__(self, start: float | None = None):
        self._fixed = start

    def now(self) -> float:
        return self._fixed if self._fixed is not None else time.time()

    def advance(self, seconds: float) -> None:
        self._fixed = self.now() + seconds

    def set(self, ts: float) -> None:
        self._fixed = ts

    def local(self) -> datetime:
        return datetime.fromtimestamp(self.now(), PHX)


def phx(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, PHX)


def parse_local(value: str) -> float:
    """'2026-10-27' or '2026-10-27T17:00' in Phoenix time -> epoch seconds."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PHX)
    return dt.timestamp()


def round_15(ts: float, up: bool = False) -> float:
    """Round to a 15 minute boundary so replies can't be used to rebuild a schedule minute by minute."""
    step = 15 * 60
    return (ts // step + (1 if up and ts % step else 0)) * step


def fmt_day(ts: float) -> str:
    return phx(ts).strftime("%a %b %-d")


def fmt_time(ts: float) -> str:
    return phx(ts).strftime("%a %-I:%M %p")


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
