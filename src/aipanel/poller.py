"""The poller: reads every backend on an interval into one Snapshot that the page, the API,
the actions' preconditions and the scheduler all share.

A backend that fails leaves its part empty and its message in `errors`; it never stops the
others, and its failure is logged once, when it starts and when it ends. The BMC is asked less
often while it and the host answer, and on every poll while either does not; its error stays
until it answers again, and only the BMC says whether the power is on.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from aipanel.backends import Backends
from aipanel.backends.energy import PowerLog
from aipanel.backends.protocols import PowerState, PricePoint, Silence
from aipanel.clock import Clock
from aipanel.config.settings import Settings
from aipanel.config.site import SiteConfig

log = logging.getLogger("aipanel.poller")

STATUS_TIMEOUT = 30.0  # longer than llm-ctl's own limit for status (20 s)
PART_TIMEOUT = 150.0  # longer than any backend's own timeout: this only catches a hang


@dataclass(frozen=True)
class GuestState:
    id: int
    name: str
    status: str
    kind: str = ""


@dataclass(frozen=True)
class HostStatus:
    hostname: str = ""
    uptime_seconds: int = 0
    kernel: str = ""
    boot_id: str = ""
    guests: tuple[GuestState, ...] = ()
    tasks: tuple[str, ...] = ()
    active_model: str | None = None
    llm_health: int | None = None  # /health's code, as llm-ctl sees it inside the container

    @classmethod
    def parse(cls, reply: dict[str, Any]) -> HostStatus:
        host = reply.get("host") or {}
        llm = reply.get("llm") or {}
        return cls(
            hostname=str(host.get("hostname", "")),
            uptime_seconds=int(host.get("uptime_seconds") or 0),
            kernel=str(host.get("kernel", "")),
            boot_id=str(host.get("boot_id", "")),
            guests=tuple(
                GuestState(
                    int(g["id"]),
                    str(g.get("name", "")),
                    str(g.get("status", "")),
                    str(g.get("type", "")),
                )
                for g in reply.get("guests") or []
            ),
            tasks=tuple(
                str(t.get("type") or t.get("upid") or t) if isinstance(t, dict) else str(t)
                for t in reply.get("tasks") or []
            ),
            active_model=llm.get("active_model"),
            llm_health=llm.get("health") if isinstance(llm.get("health"), int) else None,
        )

    def guest(self, guest_id: int) -> GuestState | None:
        return next((g for g in self.guests if g.id == guest_id), None)


@dataclass(frozen=True)
class LlmStatus:
    healthy: bool | None = None
    model: str | None = None  # the site config's name for what is loaded
    alias: str | None = None  # what /props says
    path: str | None = None
    busy: int | None = None
    slots: int | None = None
    decode_tps: float | None = None


@dataclass(frozen=True)
class Gpu:
    index: str
    temperature: float | None = None
    power: float | None = None
    memory_used: float | None = None
    memory_total: float | None = None


@dataclass(frozen=True)
class Energy:
    watts: float | None = None
    price: PricePoint | None = None
    currency: str = ""


@dataclass(frozen=True)
class Snapshot:
    at: datetime | None = None
    power: PowerState = "unknown"
    power_at: datetime | None = None  # when the BMC last answered
    host: HostStatus | None = None
    llm: LlmStatus = LlmStatus()
    gpus: tuple[Gpu, ...] = ()
    silences: tuple[Silence, ...] = ()
    energy: Energy = Energy()
    errors: dict[str, str] = field(default_factory=dict)

    def busy_reason(self) -> str | None:
        """Why stopping the host now could interrupt work. Not knowing counts as busy."""
        reasons = []
        if self.llm.busy:
            reasons.append(f"{self.llm.busy} LLM slot(s) busy")
        elif self._busy_unknown():
            why = self.errors.get("llm", "the server does not show its slots")
            reasons.append(f"the LLM's busy slots are unknown ({why})")
        if self.host is not None and self.host.tasks:
            reasons.append(f"Proxmox task running: {', '.join(self.host.tasks)}")
        return "; ".join(reasons) or None

    def _busy_unknown(self) -> bool:
        if self.host is None or self.llm.busy is not None or self.llm.healthy is False:
            return False
        # The panel could not read the LLM; inside the container it may still be serving.
        return self.host.llm_health in (None, 200)


def match_model(site: SiteConfig, alias: str | None, path: str | None) -> str | None:
    text = f"{alias or ''} {path or ''}"
    for model in site.llm.models:
        if model.match and model.match in text:
            return model.name
    return None


class Poller:
    def __init__(
        self,
        backends: Callable[[], Backends],
        site: Callable[[], SiteConfig],
        settings: Callable[[], Settings],
        clock: Clock,
        power_log: PowerLog,
    ) -> None:
        self.backends = backends
        self.site = site
        self.settings = settings
        self.clock = clock
        self.power_log = power_log
        self.snapshot = Snapshot()
        self._lock = asyncio.Lock()

    async def run(self) -> None:
        while True:
            try:
                await self.refresh(bmc=False)
            except Exception:
                log.exception("poll failed")
            await self.clock.sleep(self.settings().poll.interval_seconds)

    async def refresh(self, bmc: bool = True) -> Snapshot:
        """A new snapshot now. `bmc=False` reuses the last power reading when it is recent."""
        async with self._lock:
            prev, self.snapshot = self.snapshot, await self._poll(bmc)
            for name in sorted(prev.errors.keys() | self.snapshot.errors.keys()):
                before, now = prev.errors.get(name), self.snapshot.errors.get(name)
                if now is not None and now != before:
                    log.warning("%s: %s", name, now)
                elif now is None:
                    log.info("%s answers again", name)
            return self.snapshot

    async def _poll(self, force_bmc: bool) -> Snapshot:
        b, site, settings, now = self.backends(), self.site(), self.settings(), self.clock.now()
        prev = self.snapshot
        errors: dict[str, str] = {}

        async def guard[T](name: str, work: Awaitable[T], default: T) -> T:
            try:
                return await asyncio.wait_for(work, PART_TIMEOUT)
            except Exception as e:  # one broken backend must never stop the others
                errors[name] = str(e) or type(e).__name__
                return default

        bmc_due = (
            force_bmc
            or prev.power_at is None
            or "bmc" in prev.errors
            or prev.host is None
            or now - prev.power_at >= timedelta(seconds=settings.poll.bmc_interval_seconds)
        )
        power, power_at = prev.power, prev.power_at
        if bmc_due:
            power = await guard("bmc", b.power.status(), "unknown")
            if "bmc" not in errors:
                power_at = now

        async def nothing() -> None:
            return None

        up = power != "off"
        host, llm, decode, gpus, silences, energy = await asyncio.gather(
            guard("host", self._host(b), None) if up else nothing(),
            guard("llm", self._llm(b, site), LlmStatus()) if up else nothing(),
            guard("decode", self._decode(b, site), None) if up else nothing(),
            guard("gpus", self._gpus(b, site), ()) if up else nothing(),
            guard("silences", self._silences(b), ()),
            guard("energy", self._energy(b), Energy()),
        )
        self.power_log.observe(power, now)
        return Snapshot(
            at=now,
            power=power,
            power_at=power_at,
            host=host,
            llm=replace(llm or LlmStatus(), decode_tps=decode),
            gpus=gpus or (),
            silences=silences,
            energy=energy,
            errors=errors,
        )

    async def _host(self, b: Backends) -> HostStatus:
        return HostStatus.parse(await b.agent.call(["status"], timeout=STATUS_TIMEOUT))

    async def _llm(self, b: Backends, site: SiteConfig) -> LlmStatus:
        healthy = await b.llm.health()
        if not healthy:
            return LlmStatus(healthy=False)
        props, slots = await asyncio.gather(b.llm.props(), b.llm.slots())
        alias, path = props.get("model_alias"), props.get("model_path")
        return LlmStatus(
            healthy=True,
            model=match_model(site, alias, path),
            alias=alias,
            path=path,
            busy=slots[0] if slots else None,
            slots=slots[1] if slots else None,
        )

    async def _decode(self, b: Backends, site: SiteConfig) -> float | None:
        """Decode speed comes from Prometheus: apart, so its outage cannot hide the LLM."""
        query = site.prometheus.decode_tokens_per_second if site.prometheus else None
        if not query or b.metrics is None:
            return None
        samples = await b.metrics.query(query)
        return samples[0].value if samples else None

    async def _gpus(self, b: Backends, site: SiteConfig) -> tuple[Gpu, ...]:
        queries = site.prometheus.gpus if site.prometheus else None
        if queries is None or b.metrics is None:
            return ()
        metrics = b.metrics
        names = [
            n
            for n in ("temperature", "power", "memory_used", "memory_total")
            if getattr(queries, n)
        ]
        results = await asyncio.gather(*(metrics.query(getattr(queries, n)) for n in names))
        values: dict[str, dict[str, float]] = {}
        for name, samples in zip(names, results, strict=True):
            for sample in samples:
                values.setdefault(sample.labels.get(queries.label, "?"), {})[name] = sample.value
        return tuple(Gpu(index, **readings) for index, readings in sorted(values.items()))

    async def _silences(self, b: Backends) -> tuple[Silence, ...]:
        return tuple(await b.silences.active()) if b.silences is not None else ()

    async def _energy(self, b: Backends) -> Energy:
        watts = await b.power_source.watts() if b.power_source is not None else None
        price = await b.price_source.now() if b.price_source is not None else None
        currency = b.price_source.currency if b.price_source is not None else ""
        return Energy(watts, price, currency)
