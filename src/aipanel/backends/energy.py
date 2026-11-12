"""Where power (watts) and price (per kWh) come from: Prometheus queries, or static values.

The static kinds let a panel without Prometheus show running cost too: fixed watts while the
host is on (on/off as the panel observed it, kept in a small log), and a time-of-use tariff
from the site config.
"""

import bisect
import json
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aipanel import weekly
from aipanel.backends.protocols import Grid, Metrics, PowerState, PricePoint
from aipanel.config.site import Tariff

WEEK_SECONDS = 7 * 24 * 3600


def grid(start: datetime, end: datetime, step: int) -> range:
    """Unix times aligned to `step`, from `start` (rounded up) until before `end`."""
    first = math.ceil(start.timestamp() / step) * step
    return range(first, math.ceil(end.timestamp()), step)


class PowerLog:
    """The host's power state changes as the panel saw them: one JSON line per change."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.times: list[float] = []
        self.states: list[str] = []
        if path is not None and path.exists():
            for line in path.read_text().splitlines():
                try:
                    entry = json.loads(line)
                    self.times.append(float(entry["t"]))
                    self.states.append(str(entry["state"]))
                except ValueError, KeyError, TypeError:
                    continue

    @property
    def last(self) -> str | None:
        return self.states[-1] if self.states else None

    def observe(self, state: PowerState, at: datetime) -> None:
        if state == "unknown" or state == self.last:
            return
        self.times.append(at.timestamp())
        self.states.append(state)
        if self.path is not None:
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"t": at.timestamp(), "state": state}) + "\n")
            except OSError:
                pass  # a read-only state directory: kept in memory only

    def state_at(self, t: float) -> str | None:
        i = bisect.bisect_right(self.times, t)
        return self.states[i - 1] if i else None


class StaticPower:
    def __init__(self, watts: float, log: PowerLog) -> None:
        self.fixed = watts
        self.log = log

    async def watts(self) -> float | None:
        return self.fixed if self.log.last == "on" else None

    async def history(self, start: datetime, end: datetime, step: int) -> Grid:
        out: Grid = {}
        for t in grid(start, end, step):
            state = self.log.state_at(t)
            if state is not None:
                out[t] = self.fixed if state == "on" else None
        return out


class PrometheusPower:
    def __init__(self, metrics: Metrics, query: str) -> None:
        self.metrics = metrics
        self.query = query

    async def watts(self) -> float | None:
        samples = await self.metrics.query(self.query)
        return sum(s.value for s in samples) if samples else None

    async def history(self, start: datetime, end: datetime, step: int) -> Grid:
        steps = grid(start, end, step)
        if not steps:
            return {}
        first = datetime.fromtimestamp(steps[0], start.tzinfo)
        series = await self.metrics.query_range(self.query, first, end, step)
        summed: dict[int, float] = {}
        for s in series:
            for t, value in s.points:
                summed[round(t)] = summed.get(round(t), 0.0) + value
        # No sample means no exporter answered: the host was off.
        return {t: summed.get(t) for t in steps}


class StaticPrice:
    estimated_future = False

    def __init__(self, tariff: Tariff, tz: ZoneInfo, now: Callable[[], datetime]) -> None:
        self.tariff = tariff
        self.tz = tz
        self.clock_now = now
        self.currency = tariff.currency
        self._windows = [(w, weekly.spans(w.days, w.start, w.end)) for w in tariff.windows]

    def price_at(self, when: datetime) -> PricePoint:
        local = when.astimezone(self.tz)
        for window, spans in self._windows:
            if window.months and local.month not in window.months:
                continue
            if weekly.contains(spans, local):
                return PricePoint(self.tariff.periods[window.period], window.period)
        return PricePoint(self.tariff.periods[self.tariff.default], self.tariff.default)

    async def now(self) -> PricePoint:
        return self.price_at(self.clock_now())

    async def history(self, start: datetime, end: datetime, step: int) -> dict[int, PricePoint]:
        return {
            t: self.price_at(datetime.fromtimestamp(t, self.tz)) for t in grid(start, end, step)
        }

    async def future(self, start: datetime, end: datetime, step: int) -> dict[int, PricePoint]:
        return await self.history(start, end, step)


class PrometheusPrice:
    """Prices from a query. Its future is unknown: the same time one week earlier stands in."""

    estimated_future = True

    def __init__(
        self,
        metrics: Metrics,
        query: str,
        period_label: str | None,
        currency: str,
        now: Callable[[], datetime],
    ) -> None:
        self.metrics = metrics
        self.query = query
        self.period_label = period_label
        self.currency = currency
        self.clock_now = now

    def _period(self, labels: dict[str, str]) -> str | None:
        return labels.get(self.period_label) if self.period_label else None

    async def now(self) -> PricePoint:
        samples = await self.metrics.query(self.query)
        if not samples:
            raise LookupError("the price query returned nothing")
        best = max(samples, key=lambda s: s.value)
        return PricePoint(best.value, self._period(best.labels))

    async def history(self, start: datetime, end: datetime, step: int) -> dict[int, PricePoint]:
        steps = grid(start, end, step)
        if not steps:
            return {}
        first = datetime.fromtimestamp(steps[0], start.tzinfo)
        out: dict[int, PricePoint] = {}
        # One series per period when the query keeps the period label: merge them.
        for s in await self.metrics.query_range(self.query, first, end, step):
            for t, value in s.points:
                out[round(t)] = PricePoint(value, self._period(s.labels))
        return out

    async def future(self, start: datetime, end: datetime, step: int) -> dict[int, PricePoint]:
        now = self.clock_now()
        past = await self.history(now - timedelta(seconds=WEEK_SECONDS), now, step)
        if not past:
            return {}
        times = sorted(past)
        out: dict[int, PricePoint] = {}
        for t in grid(start, end, step):
            weeks = 1 + max(0, math.floor((t - now.timestamp()) / WEEK_SECONDS))
            back = t - weeks * WEEK_SECONDS
            i = bisect.bisect_right(times, back)
            out[t] = past[times[i - 1]] if i else past[times[0]]
        return out
