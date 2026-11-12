"""Running cost: a static tariff, history from observed power, savings and the forecast."""

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeClock, MakePanel, lab, site_config

from aipanel.backends.energy import PowerLog, PrometheusPrice, StaticPrice, grid
from aipanel.backends.protocols import PricePoint, Sample, Series
from aipanel.config.site import Tariff
from aipanel.cost import STEP, integrate, percentile

pytestmark = pytest.mark.anyio
UTC_ZONE = ZoneInfo("UTC")
WED = datetime(2026, 3, 4, tzinfo=UTC)

TOU = Tariff.model_validate(
    {
        "currency": "USD",
        "periods": {"peak": 0.5, "night": 0.2, "day": 0.3, "summer-peak": 0.7},
        "default": "day",
        "windows": [
            {
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "from": "17:00",
                "to": "21:00",
                "period": "summer-peak",
                "months": [6, 7, 8],
            },
            {
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "from": "17:00",
                "to": "21:00",
                "period": "peak",
            },
            {
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "from": "23:00",
                "to": "06:00",
                "period": "night",
            },
        ],
    }
)
FLAT = {"currency": "USD", "periods": {"flat": 0.25}, "default": "flat"}


@pytest.mark.parametrize(
    ("when", "period"),
    [
        (WED.replace(hour=18), "peak"),
        (WED.replace(hour=23, minute=30), "night"),
        (WED.replace(day=5, hour=3), "night"),  # Wednesday's night window, after midnight
        (WED.replace(day=7, hour=18), "day"),  # a Saturday: no peak
        (WED.replace(month=7, day=1, hour=18), "summer-peak"),  # a Wednesday in July
        (WED.replace(hour=12), "day"),
    ],
)
def test_static_tariff(when: datetime, period: str) -> None:
    point = StaticPrice(TOU, UTC_ZONE, lambda: when).price_at(when)
    assert point == PricePoint(TOU.periods[period], period)


def test_integrate_on_off_and_savings() -> None:
    steps = range(0, 2 * 3600, STEP)  # two hours
    power: dict[int, float | None] = {t: 1200.0 if t < 3600 else None for t in steps}
    prices = {t: PricePoint(0.3, "day") for t in steps}
    total = integrate(steps, power, prices, idle=200.0, standby=20.0)
    assert total.kwh == pytest.approx(1.2) and total.cost == pytest.approx(0.36)
    assert total.hours_on == pytest.approx(1) and total.hours_off == pytest.approx(1)
    assert total.saved == pytest.approx(0.18 * 0.3)
    assert total.by_period == {"day": pytest.approx(0.36)}


def test_unknown_steps_count_neither_way() -> None:
    steps = range(0, 3600, STEP)
    total = integrate(steps, {}, {t: PricePoint(1.0) for t in steps}, idle=100.0, standby=0.0)
    assert total.hours_on == total.hours_off == total.cost == total.saved == 0


def test_percentile() -> None:
    assert percentile([], 0.1) is None
    assert percentile([float(v) for v in range(1, 101)], 0.10) == 10


async def test_static_power_and_tariff(
    make_panel: MakePanel, clock: FakeClock, tmp_path: Any
) -> None:
    site = site_config(
        cost={
            "power": {"source": "static", "watts": 400},
            "price": {"source": "static", "tariff": FLAT},
        }
    )
    panel = make_panel(site)
    log = panel.power_log
    log.times, log.states = [], []
    for hours, state in ((0, "on"), (6, "off"), (8, "on")):
        log.observe(state, WED + timedelta(hours=hours))  # type: ignore[arg-type]
    clock.set(WED.replace(hour=12))
    panel.store.update(
        {
            "cost.idle_watts": 200,
            "schedule.rules": [
                {
                    "name": "nights",
                    "days": ["mon", "tue", "wed", "thu", "fri"],
                    "off": "23:00",
                    "on": "07:00",
                }
            ],
        },
        "t",
    )

    report = await panel.cost.report(await panel.poller.refresh())
    assert report.error is None and report.currency == "USD"
    assert report.today.hours_on == pytest.approx(10) and report.today.hours_off == pytest.approx(2)
    assert report.today.kwh == pytest.approx(4.0) and report.today.cost == pytest.approx(1.0)
    assert report.today.saved == pytest.approx(2 * 0.185 * 0.25)
    assert report.today.by_period == {"flat": pytest.approx(1.0)}
    assert report.month.cost == pytest.approx(report.today.cost)  # nothing logged before today
    assert report.forecast_off_hours == pytest.approx(176)  # 22 weeknights of 8 hours
    assert report.forecast_saving == pytest.approx(176 * 0.185 * 0.25)
    assert report.estimated_future is False


async def test_now_while_off_uses_standby(panel: Any) -> None:
    lab(panel).bmc("off")
    now = panel.cost.now(await panel.poller.refresh())
    assert now.watts == 15 and now.estimated
    assert now.per_hour == pytest.approx(15 / 1000 * now.price.price)


async def test_demo_report_reads_prometheus(panel: Any) -> None:
    report = await panel.cost.report(await panel.poller.refresh())
    assert report.error is None and report.estimated_future
    assert set(report.week.by_period) == {"peak", "part-peak", "off-peak"}
    assert report.idle_source.startswith("10th percentile") and report.idle_watts
    assert report.week.hours_off > 0 and report.month.saved > 0


class WeekOfPrices:
    """A price series for Prometheus: 0.10 plus a tenth of a cent per hour since WED."""

    async def query(self, promql: str) -> list[Sample]:
        return [Sample({"period": "x"}, 0.5)]

    async def query_range(
        self, promql: str, start: datetime, end: datetime, step: int
    ) -> list[Series]:
        points = [
            (float(t), 0.10 + (t - WED.timestamp()) / 3600 / 1000)
            for t in grid(start, end + timedelta(seconds=1), step)
        ]
        return [Series({"period": "x"}, points)]


async def test_prometheus_future_prices_repeat_last_week() -> None:
    now = WED + timedelta(days=7)
    price = PrometheusPrice(WeekOfPrices(), "q", "period", "USD", lambda: now)
    future = await price.future(now, now + timedelta(days=9), 3600)
    in_three_days = int((now + timedelta(days=3)).timestamp())
    in_eight_days = int((now + timedelta(days=8)).timestamp())
    last_week = (await price.history(WED, now, 3600))[in_three_days - 7 * 86400]
    assert future[in_three_days] == last_week
    assert future[in_eight_days] == future[in_eight_days - 7 * 86400]
    assert PowerLog(None).state_at(0) is None
