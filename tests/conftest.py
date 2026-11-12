"""Shared fixtures: a fake clock, the demo lab, and a panel built on them."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aipanel.backends.demo import DemoLab, demo_site_config
from aipanel.config.loader import SiteConfigLoader
from aipanel.config.site import SiteConfig
from aipanel.config.store import merge
from aipanel.panel import Panel

# A Wednesday, noon UTC (the demo site's time zone).
START = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)


class FakeClock:
    """Time that moves only when someone sleeps, or a test moves it."""

    def __init__(self, start: datetime = START) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    def set(self, when: datetime) -> None:
        self.t = when

    def advance(self, **delta: float) -> None:
        self.t += timedelta(**delta)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def site_config(**changes: Any) -> SiteConfig:
    """The demo site config with changes: a top-level key replaces that section, and a dotted
    key (`"actions.dry_run": True`) changes one value."""
    data = demo_site_config().model_dump(mode="json", by_alias=True, exclude_none=True)
    data |= {k: v for k, v in changes.items() if "." not in k}
    return SiteConfig.model_validate(merge(data, {k: v for k, v in changes.items() if "." in k}))


MakePanel = Callable[..., Panel]


@pytest.fixture
def make_panel(tmp_path: Path, clock: FakeClock) -> MakePanel:
    def make(site: SiteConfig | None = None, state: Path | None = None) -> Panel:
        loader = SiteConfigLoader(None, demo=True, initial=site or site_config())
        panel = Panel(loader, state or tmp_path / "state", demo=True, clock=clock)
        lab(panel).busy = 0  # the demo's busy pattern would make tests depend on the time
        return panel

    return make


@pytest.fixture
def panel(make_panel: MakePanel) -> Panel:
    return make_panel()


def lab(panel: Panel) -> DemoLab:
    assert panel.lab is not None
    return panel.lab
