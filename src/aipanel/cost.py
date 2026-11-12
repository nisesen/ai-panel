"""Running cost: now, history, savings and the schedule's forecast.

History is watts times price, integrated over 5-minute steps. A step without a power reading
counts as off. Savings are the off hours at what the host would have drawn idle, minus what it
draws in standby, at each step's price. Idle watts are a setting, or the 10th percentile of the
last 7 days' readings while on.
"""

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from aipanel.backends import Backends
from aipanel.backends.energy import grid
from aipanel.backends.protocols import BackendError, Grid, PricePoint
from aipanel.clock import Clock
from aipanel.config.settings import Settings
from aipanel.config.site import SiteConfig
from aipanel.poller import Snapshot
from aipanel.scheduler import boundaries

STEP = 300
CACHE_SECONDS = 300
HOURS = STEP / 3600


@dataclass(frozen=True)
class Now:
    watts: float | None
    estimated: bool  # the host is off: these are the standby watts
    price: PricePoint | None
    per_hour: float | None
    per_day: float | None


@dataclass
class Total:
    kwh: float = 0.0
    cost: float = 0.0
    hours_on: float = 0.0
    hours_off: float = 0.0
    saved: float = 0.0
    by_period: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Report:
    currency: str
    now: Now
    today: Total
    week: Total
    month: Total
    idle_watts: float | None
    idle_source: str
    standby_watts: float
    forecast_saving: float | None
    forecast_off_hours: float
    estimated_future: bool
    computed_at: datetime
    error: str | None = None


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]


def integrate(
    steps: range,
    power: Grid,
    prices: dict[int, PricePoint],
    idle: float | None,
    standby: float,
) -> Total:
    total = Total()
    price: PricePoint | None = None
    for t in steps:
        price = prices.get(t, price)  # a gap in the price series keeps the last price
        if t not in power:
            continue  # unknown: neither on nor off
        watts = power[t]
        if watts is None:
            total.hours_off += HOURS
            if idle is not None and price is not None:
                total.saved += max(0.0, idle - standby) * HOURS / 1000 * price.price
            continue
        total.hours_on += HOURS
        kwh = watts * HOURS / 1000
        total.kwh += kwh
        if price is not None:
            total.cost += kwh * price.price
            period = price.period or "all"
            total.by_period[period] = total.by_period.get(period, 0.0) + kwh * price.price
    return total


class CostService:
    def __init__(
        self,
        backends: Callable[[], Backends],
        site: Callable[[], SiteConfig],
        settings: Callable[[], Settings],
        clock: Clock,
    ) -> None:
        self.backends = backends
        self.site = site
        self.settings = settings
        self.clock = clock
        self._cache: Report | None = None
        self._lock = asyncio.Lock()

    def now(self, snapshot: Snapshot) -> Now:
        standby = self.settings().cost.standby_watts
        watts, estimated = snapshot.energy.watts, False
        if watts is None and snapshot.power == "off":
            watts, estimated = standby, True
        price = snapshot.energy.price
        per_hour = watts / 1000 * price.price if watts is not None and price else None
        return Now(
            watts, estimated, price, per_hour, per_hour * 24 if per_hour is not None else None
        )

    async def report(self, snapshot: Snapshot, refresh: bool = False) -> Report:
        async with self._lock:
            now = self.clock.now()
            cached = self._cache
            if (
                cached
                and not refresh
                and (now - cached.computed_at).total_seconds() < CACHE_SECONDS
            ):
                return cached
            self._cache = await self._compute(snapshot, now)
            return self._cache

    async def _compute(self, snapshot: Snapshot, now: datetime) -> Report:
        b, site, settings = self.backends(), self.site(), self.settings()
        tz = site.site.tz
        local = now.astimezone(tz)
        today = local.replace(hour=0, minute=0, second=0, microsecond=0)
        month = today.replace(day=1)
        week = now - timedelta(days=7)
        standby = settings.cost.standby_watts
        empty = Report(
            currency=b.price_source.currency if b.price_source else "",
            now=self.now(snapshot),
            today=Total(),
            week=Total(),
            month=Total(),
            idle_watts=settings.cost.idle_watts,
            idle_source="setting",
            standby_watts=standby,
            forecast_saving=None,
            forecast_off_hours=0.0,
            estimated_future=False,
            computed_at=now,
        )
        if b.power_source is None or b.price_source is None:
            return replace(empty, error="no cost section in the site config")
        start = min(month, week)
        try:
            power = await b.power_source.history(start, now, STEP)
            prices = await b.price_source.history(start, now, STEP)
        except (BackendError, LookupError, OSError, ValueError) as e:
            return replace(empty, error=f"history unavailable: {e}")

        idle, source = settings.cost.idle_watts, "setting"
        if idle is None:
            readings = [w for t, w in power.items() if w is not None and t >= week.timestamp()]
            idle, source = percentile(readings, 0.10), "10th percentile of 7 days"

        def total(since: datetime) -> Total:
            return integrate(grid(since, now, STEP), power, prices, idle, standby)

        hours, saving = await self._forecast(b, site, settings, now, idle, standby)
        return Report(
            currency=b.price_source.currency,
            now=self.now(snapshot),
            today=total(today),
            week=total(week),
            month=total(month),
            idle_watts=idle,
            idle_source=source if idle is not None else "unknown: no readings yet",
            standby_watts=standby,
            forecast_saving=saving,
            forecast_off_hours=hours,
            estimated_future=b.price_source.estimated_future,
            computed_at=now,
        )

    async def _forecast(
        self,
        b: Backends,
        site: SiteConfig,
        settings: Settings,
        now: datetime,
        idle: float | None,
        standby: float,
    ) -> tuple[float, float | None]:
        """Off hours of the schedule's rules over the next 30 days, and what they save."""
        assert b.price_source is not None
        end = now + timedelta(days=30)
        rules = settings.schedule.rules
        found = boundaries(rules, site.site.tz, now - timedelta(days=2), end + timedelta(days=2))
        off_steps: set[int] = set()
        for off in (x for x in found if x.kind == "off"):
            on = next(
                (x for x in found if x.kind == "on" and x.rule == off.rule and x.at > off.at), None
            )
            if on is not None:
                off_steps.update(grid(max(off.at, now), min(on.at, end), STEP))
        hours = len(off_steps) * HOURS
        if idle is None or not off_steps:
            return hours, None if idle is None else 0.0
        try:
            prices = await b.price_source.future(now, end, STEP)
        except BackendError, LookupError, OSError, ValueError:
            return hours, None
        rate = max(0.0, idle - standby) * HOURS / 1000
        return hours, sum(rate * prices[t].price for t in off_steps if t in prices)
