"""The panel: builds every part from the site config and runs the background loops."""

import asyncio
import contextlib
import logging
from pathlib import Path

import httpx

from aipanel.audit import Audit
from aipanel.auth import load_csrf_key
from aipanel.backends import Backends
from aipanel.backends.agent import SshAgent
from aipanel.backends.demo import (
    DemoAgent,
    DemoLab,
    DemoLlm,
    DemoMetrics,
    DemoPower,
    DemoSilences,
)
from aipanel.backends.energy import (
    PowerLog,
    PrometheusPower,
    PrometheusPrice,
    StaticPower,
    StaticPrice,
)
from aipanel.backends.llm import LlamaCpp
from aipanel.backends.metrics import Prometheus
from aipanel.backends.power import IpmiPower
from aipanel.backends.protocols import Metrics, PowerSource, PriceSource
from aipanel.backends.silences import Alertmanager
from aipanel.clock import Clock, SystemClock
from aipanel.config import site as cfg
from aipanel.config.loader import SiteConfigLoader
from aipanel.config.site import SiteConfig
from aipanel.config.store import SettingsStore
from aipanel.cost import CostService
from aipanel.files import writable
from aipanel.metrics import PanelMetrics
from aipanel.poller import Poller
from aipanel.runner import Runner
from aipanel.scheduler import Scheduler

log = logging.getLogger("aipanel")

WATCH_SECONDS = 5
BACKENDS = ("bmc", "host", "llm", "gpus", "silences", "energy")


class Panel:
    def __init__(
        self,
        loader: SiteConfigLoader,
        state_dir: Path,
        demo: bool = False,
        clock: Clock | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.loader = loader
        self.demo = demo
        self.clock = clock or SystemClock()
        self.state_dir = state_dir
        with contextlib.suppress(OSError):
            state_dir.mkdir(parents=True, exist_ok=True)
        self.state_writable = writable(state_dir)
        if not self.state_writable:
            log.warning("state directory %s is not writable: read-only mode", state_dir)

        def in_state(name: str) -> Path | None:
            return state_dir / name if self.state_writable else None

        self.audit = Audit(in_state("audit.jsonl"))
        self.store = SettingsStore(state_dir / "settings.json", self.audit, lambda: self.site)
        self.metrics = PanelMetrics()
        self.power_log = PowerLog(in_state("power.jsonl"))
        self.http = http or httpx.AsyncClient()
        self.lab = DemoLab(self.site, self.clock) if demo else None
        self.backends = self._build_backends()
        self.poller = Poller(
            lambda: self.backends, lambda: self.site, self.store.get, self.clock, self.power_log
        )
        self.runner = Runner(
            lambda: self.site,
            self.store.get,
            lambda: self.backends,
            self.poller,
            self.clock,
            self.audit,
            self.metrics,
        )
        self.scheduler = Scheduler(
            in_state("scheduler.json"),
            lambda: self.site,
            self.store.get,
            self.runner,
            self.poller,
            self.clock,
            self.audit,
            self.metrics,
        )
        self.cost = CostService(
            lambda: self.backends, lambda: self.site, self.store.get, self.clock
        )
        self.csrf_key = load_csrf_key(state_dir / "csrf.key")
        self.flash: dict[str, tuple[str, str]] = {}  # user -> (kind, message) for the next page
        self._tasks: list[asyncio.Task[None]] = []
        self.update_status_metrics()

    @property
    def site(self) -> SiteConfig:
        return self.loader.current

    def _build_backends(self) -> Backends:
        site = self.site
        if self.lab is not None:
            self.lab.site = site
            metrics: Metrics | None = DemoMetrics(self.lab)
            power_source, price_source = self._energy(site, metrics)
            return Backends(
                DemoPower(self.lab),
                DemoAgent(self.lab),
                DemoLlm(self.lab),
                metrics,
                DemoSilences(
                    self.clock, len(site.alertmanager.silences) if site.alertmanager else 1
                ),
                power_source,
                price_source,
            )
        metrics = Prometheus(site.prometheus, self.http) if site.prometheus else None
        power_source, price_source = self._energy(site, metrics)
        return Backends(
            IpmiPower(site.bmc),
            SshAgent(site.host),
            LlamaCpp(site.llm, self.http),
            metrics,
            Alertmanager(site.alertmanager, self.http) if site.alertmanager else None,
            power_source,
            price_source,
        )

    def _energy(
        self, site: SiteConfig, metrics: Metrics | None
    ) -> tuple[PowerSource | None, PriceSource | None]:
        if site.cost is None:
            return None, None
        power: PowerSource | None = None
        price: PriceSource | None = None
        match site.cost.power:
            case cfg.StaticPower(watts=watts):
                power = StaticPower(watts, self.power_log)
            case cfg.PrometheusPower(query=query) if metrics is not None:
                power = PrometheusPower(metrics, query)
        match site.cost.price:
            case cfg.StaticPrice(tariff=tariff):
                price = StaticPrice(tariff, site.site.tz, self.clock.now)
            case cfg.PrometheusPrice() as c if metrics is not None:
                price = PrometheusPrice(
                    metrics, c.query, c.period_label, c.currency, self.clock.now
                )
        return power, price

    def watch(self) -> None:
        """Pick up a changed site config or settings file, and update the status metrics."""
        if self.loader.reload_if_changed():
            self.backends = self._build_backends()
        self.store.reload_if_changed()
        self.update_status_metrics()

    def update_status_metrics(self) -> None:
        m, snap = self.metrics, self.poller.snapshot
        m.set("aipanel_config_valid", 1 if self.loader.valid else 0)
        settings_ok = self.store.error is None and self.store.conflict() is None
        m.set("aipanel_settings_valid", 1 if settings_ok else 0)
        m.set("aipanel_state_writable", 1 if self.state_writable else 0)
        if snap.at is not None:
            m.set("aipanel_host_power_on", {"on": 1, "off": 0}.get(snap.power, -1))
            m.set("aipanel_host_reachable", 1 if snap.host is not None else 0)
            for name in BACKENDS:
                m.set("aipanel_backend_up", 0 if name in snap.errors else 1, {"backend": name})

    async def _watch_loop(self) -> None:
        while True:
            try:
                self.watch()
            except Exception:
                log.exception("watch failed")
            await self.clock.sleep(WATCH_SECONDS)

    async def start(self) -> None:
        from aipanel import __version__

        self.metrics.set("aipanel_build_info", 1, {"version": __version__})
        self._tasks = [
            asyncio.create_task(self.poller.run(), name="poller"),
            asyncio.create_task(self.scheduler.run(), name="scheduler"),
            asyncio.create_task(self._watch_loop(), name="watch"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.runner.stop()
        await self.http.aclose()
