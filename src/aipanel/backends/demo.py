"""DEMO mode: a simulated GPU host behind every interface, for the demo and for the tests.

`DemoLab` is the machine: BMC power, the OS, guests, the LLM, two GPUs. It moves on with the
clock (a boot takes `boot_seconds`), so a demo shows actions step by step and a test with a
fake clock runs them instantly. Its failure switches (`never_boots`, `ssh_down`, ...) let the
tests drive every action into a timeout or a refusal.
"""

import itertools
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from aipanel.backends.energy import PowerLog, StaticPrice
from aipanel.backends.protocols import (
    BackendError,
    PowerState,
    Sample,
    Sensor,
    Series,
    Silence,
    Unreachable,
)
from aipanel.clock import Clock
from aipanel.config.site import SiteConfig, Tariff

GIB = 1024**3


def demo_site_config() -> SiteConfig:
    """The demo's site config. Addresses are from the documentation ranges."""
    everyday = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return SiteConfig.model_validate(
        {
            "site": {
                "name": "gpu-demo",
                "timezone": "UTC",
                "links": [
                    {"label": "Dashboard", "url": "https://dashboard.example.com"},
                    {"label": "Grafana", "url": "https://grafana.example.com"},
                ],
            },
            "auth": {"mode": "none"},
            "bmc": {
                "host": "192.0.2.10",
                "username": {"env": "DEMO_BMC_USER"},
                "password": {"env": "DEMO_BMC_PASSWORD"},
            },
            "host": {
                "address": "192.0.2.20",
                "key_file": "/dev/null",
                "known_hosts_file": "/dev/null",
                "guests": [
                    {"id": 210, "name": "llm"},
                    {"id": 211, "name": "monitor"},
                    {"id": 212, "name": "chat"},
                ],
                "llm_guest": 210,
            },
            "llm": {
                "url": "http://192.0.2.21:8080",
                "models": [
                    {"name": "dense", "label": "Dense 32B", "match": "dense"},
                    {"name": "moe", "label": "MoE 30B-A3B", "match": "moe"},
                ],
                "pause_client_checks": True,
            },
            "prometheus": {
                "url": "http://prometheus.example.com:9090",
                "gpus": {
                    "temperature": "demo_gpu_temperature_celsius",
                    "power": "demo_gpu_power_watts",
                    "memory_used": "demo_gpu_memory_used_bytes",
                    "memory_total": "demo_gpu_memory_total_bytes",
                },
                "decode_tokens_per_second": "demo_decode_tokens_per_second",
            },
            "alertmanager": {
                "url": "http://alertmanager.example.com:9093",
                "silences": [
                    {"matchers": [{"name": "instance", "value": "gpu-demo.*", "is_regex": True}]},
                    {"matchers": [{"name": "alertname", "value": "LlmDown"}]},
                ],
            },
            "cost": {
                "power": {"source": "prometheus", "query": "demo_host_power_watts"},
                "price": {"source": "prometheus", "query": "demo_price_per_kwh"},
            },
            "protected_windows": [
                {"days": everyday, "from": "21:00", "to": "22:30", "reason": "nightly backups"}
            ],
        }
    )


DEMO_RULE = {
    "name": "weeknights",
    "days": ["mon", "tue", "wed", "thu", "fri"],
    "off": "23:00",
    "on": "07:00",
}

DEMO_TARIFF = Tariff.model_validate(
    {
        "currency": "USD",
        "periods": {"peak": 0.45, "part-peak": 0.34, "off-peak": 0.26},
        "default": "off-peak",
        "windows": [
            {
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "from": "16:00",
                "to": "21:00",
                "period": "peak",
            },
            {
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "from": "15:00",
                "to": "00:00",
                "period": "part-peak",
            },
        ],
    }
)


def _noise(t: float, salt: int = 0) -> float:
    """A repeatable value in [0, 1) for a 5-minute slot."""
    return ((int(t // 300) * 2654435761 + salt * 40503) % 1000) / 1000


class DemoLab:
    boot_seconds = 40.0
    llm_load_seconds = 15.0
    shutdown_seconds = 15.0

    def __init__(self, site: SiteConfig, clock: Clock) -> None:
        self.site = site
        self.clock = clock
        now = clock.now()
        self.started_at = now
        self.power: PowerState = "on"
        self.os_up_at: datetime | None = now - timedelta(days=3)
        self.off_at: datetime | None = None  # a shutdown in progress ends at this time
        self.boot_count = 1
        self.models = [m.name for m in site.llm.models]
        self.model: str | None = self.models[0] if self.models else None
        self.llm_ready_at: datetime | None = self.os_up_at
        self.stopped_guests: set[int] = set()
        self.tasks: list[dict[str, Any]] = []
        self.busy: int | None = None  # None: a pattern over time; a number pins it
        self.checks_paused = False
        self.transitions: list[tuple[float, str]] = []
        self.calls: list[str] = []
        # Failure switches for the tests
        self.never_boots = False
        self.never_powers_off = False
        self.llm_never_ready = False
        self.ssh_down = False
        self.bmc_down = False
        self.refuse: set[str] = set()  # agent verbs that answer with an error

    # The machine moving on with time
    def _now(self) -> datetime:
        now = self.clock.now()
        if self.off_at is not None and now >= self.off_at and not self.never_powers_off:
            self._power_off(self.off_at)
        return now

    def _power_off(self, at: datetime) -> None:
        self.power, self.os_up_at, self.off_at, self.llm_ready_at = "off", None, None, None
        self.transitions.append((at.timestamp(), "off"))

    def _boot(self, at: datetime, delay: float) -> None:
        self.boot_count += 1
        self.os_up_at = None if self.never_boots else at + timedelta(seconds=delay)
        self.stopped_guests.clear()
        self.model = self.models[0] if self.models else None
        if self.os_up_at is not None:
            self.llm_ready_at = self.os_up_at + timedelta(seconds=self.llm_load_seconds)

    def os_up(self) -> bool:
        now = self._now()
        return (
            self.power == "on"
            and self.off_at is None
            and self.os_up_at is not None
            and now >= self.os_up_at
            and not self.ssh_down
        )

    def llm_healthy(self) -> bool:
        now = self._now()
        return (
            self.os_up()
            and self.model is not None
            and self.llm_ready_at is not None
            and now >= self.llm_ready_at
            and not self.llm_never_ready
        )

    def busy_slots(self) -> int:
        if self.busy is not None:
            return self.busy
        return 1 if _noise(self.clock.now().timestamp(), 7) < 0.3 else 0

    def guest_status(self, guest_id: int) -> str:
        return "stopped" if guest_id in self.stopped_guests or not self.os_up() else "running"

    def watts_at(self, t: float) -> float:
        busy = _noise(t, 3) < 0.35
        return round(205 + 30 * _noise(t, 5) + (240 if busy else 0), 1)

    # BMC
    def bmc(self, command: str) -> None:
        now = self._now()
        self.calls.append(f"bmc {command}")
        if command == "on" and self.power == "off":
            self.power = "on"
            self.transitions.append((now.timestamp(), "on"))
            self._boot(now, self.boot_seconds)
        elif command == "soft" and self.power == "on":
            self.off_at = now + timedelta(seconds=self.shutdown_seconds)
        elif command == "off" and self.power == "on":
            self._power_off(now)
        elif command == "reset" and self.power == "on":
            self._boot(now, self.boot_seconds)

    # The host agent's verbs, as llm-ctl answers them
    def agent(self, args: Sequence[str]) -> dict[str, Any]:
        now = self._now()
        if not self.os_up():
            raise Unreachable("host agent: ssh: connect to host 192.0.2.20 port 22: timed out")
        self.calls.append(" ".join(args))
        verb = args[0]
        if verb in self.refuse:
            raise BackendError(f"host agent: {verb} failed (demo)")
        match list(args):
            case ["status"]:
                return self._status(now)
            case ["shutdown"]:
                self.off_at = now + timedelta(seconds=self.shutdown_seconds)
                return {"ok": True, "verb": "shutdown"}
            case ["reboot"]:
                self.off_at = None
                self._boot(now, self.shutdown_seconds + self.boot_seconds)
                return {"ok": True, "verb": "reboot"}
            case ["llm", "switch", model] if model in self.models:
                self.model, self.llm_ready_at = (
                    model,
                    now + timedelta(seconds=self.llm_load_seconds),
                )
                return {"ok": True, "verb": "llm switch", "model": model}
            case ["llm", "restart"] if self.model is not None:
                self.llm_ready_at = now + timedelta(seconds=self.llm_load_seconds)
                return {"ok": True, "verb": "llm restart", "model": self.model}
            case ["llm", "start"]:
                self.model = self.model or (self.models[0] if self.models else None)
                self.llm_ready_at = now + timedelta(seconds=self.llm_load_seconds)
                return {"ok": True, "verb": "llm start", "model": self.model}
            case ["llm", "stop"]:
                self.model = None
                return {"ok": True, "verb": "llm stop"}
            case ["guest", gid, op] if gid.isdigit() and self.site.guest(int(gid)):
                return self._guest(int(gid), op)
            case ["probe"]:
                gpus = [f"{g['index']}, {g['temperature']} C, {g['power']} W" for g in self.gpus()]
                return {"ok": True, "verb": "probe", "gpus": gpus, "health": self.llm_healthy()}
            case ["client-checks", "pause" | "resume" as op]:
                self.checks_paused = op == "pause"
                return {"ok": True, "verb": "client-checks", "op": op}
        raise BackendError(f"host agent: refused {' '.join(args)!r}")

    def _guest(self, guest_id: int, op: str) -> dict[str, Any]:
        if op == "start":
            self.stopped_guests.discard(guest_id)
        elif op == "shutdown":
            self.stopped_guests.add(guest_id)
            if guest_id == self.site.host.llm_guest:
                self.model = None
        elif op == "reboot":
            self.stopped_guests.discard(guest_id)
        else:
            raise BackendError(f"host agent: unknown guest operation {op!r}")
        return {"ok": True, "verb": "guest", "id": guest_id, "op": op}

    def _status(self, now: datetime) -> dict[str, Any]:
        assert self.os_up_at is not None
        return {
            "ok": True,
            "verb": "status",
            "host": {
                "hostname": self.site.site.name,
                "uptime_seconds": int((now - self.os_up_at).total_seconds()),
                "kernel": "6.17.2-1-pve",
                "boot_id": f"demo-boot-{self.boot_count}",
            },
            "guests": [
                {
                    "id": g.id,
                    "name": g.name,
                    "type": "lxc" if g.id < 212 else "qemu",
                    "status": self.guest_status(g.id),
                }
                for g in self.site.host.guests
            ],
            "tasks": list(self.tasks),
            "llm": {"active_model": self.model, "health": 200 if self.llm_healthy() else 503},
        }

    def gpus(self) -> list[dict[str, Any]]:
        t = self.clock.now().timestamp()
        loaded = self.llm_healthy()
        load = 0.2 + 0.7 * self.busy_slots() if loaded else 0.0
        return (
            [
                {
                    "index": str(i),
                    "temperature": round(36 + 30 * load + 4 * _noise(t, i), 1),
                    "power": round(52 + 170 * load + 10 * _noise(t, i + 2), 1),
                    "memory_used": (26.8 + 0.4 * i) * GIB if loaded else 0.5 * GIB,
                    "memory_total": 40 * GIB,
                }
                for i in range(2)
            ]
            if self.os_up()
            else []
        )


class DemoPower:
    def __init__(self, lab: DemoLab) -> None:
        self.lab = lab

    def _reachable(self) -> None:
        if self.lab.bmc_down:
            raise Unreachable("BMC: no response (demo)")

    async def status(self) -> PowerState:
        self._reachable()
        self.lab.os_up()  # lets a shutdown in progress finish
        return self.lab.power

    async def on(self) -> None:
        self._reachable()
        self.lab.bmc("on")

    async def soft(self) -> None:
        self._reachable()
        self.lab.bmc("soft")

    async def off(self) -> None:
        self._reachable()
        self.lab.bmc("off")

    async def reset(self) -> None:
        self._reachable()
        self.lab.bmc("reset")

    async def sensors(self) -> list[Sensor]:
        self._reachable()
        on = self.lab.power == "on"
        return [
            Sensor("CPU Temp", "46 degrees C" if on else "no reading", "ok"),
            Sensor("System Temp", "31 degrees C", "ok"),
            Sensor("PSU1 Status", "0x01", "ok"),
        ]

    async def events(self, limit: int = 10) -> list[str]:
        self._reachable()
        return ["1 | 01/01/2026 | 00:00:00 | Power Unit | Power off/down | Asserted"][:limit]


class DemoAgent:
    def __init__(self, lab: DemoLab) -> None:
        self.lab = lab

    async def call(self, args: Sequence[str], timeout: float) -> dict[str, Any]:
        return self.lab.agent(args)


class DemoLlm:
    def __init__(self, lab: DemoLab) -> None:
        self.lab = lab

    def _reachable(self) -> None:
        if not self.lab.os_up():
            raise Unreachable("LLM: ConnectTimeout")

    async def health(self) -> bool:
        self._reachable()
        return self.lab.llm_healthy()

    async def props(self) -> dict[str, Any]:
        self._reachable()
        if not self.lab.llm_healthy() or self.lab.model is None:
            raise BackendError("LLM /props: HTTP 503")
        name = self.lab.model
        return {"model_alias": f"demo-{name}", "model_path": f"/models/{name}-Q8_0.gguf"}

    async def slots(self) -> tuple[int, int] | None:
        self._reachable()
        return (self.lab.busy_slots(), 4) if self.lab.llm_healthy() else None


class DemoMetrics:
    """Answers the demo config's queries; anything else returns nothing."""

    def __init__(self, lab: DemoLab) -> None:
        self.lab = lab
        self.tariff = StaticPrice(DEMO_TARIFF, lab.site.site.tz, lab.clock.now)
        self._gpu_fields = {
            "demo_gpu_temperature_celsius": "temperature",
            "demo_gpu_power_watts": "power",
            "demo_gpu_memory_used_bytes": "memory_used",
            "demo_gpu_memory_total_bytes": "memory_total",
        }

    def on_at(self, t: float) -> bool:
        if t >= self.lab.started_at.timestamp():
            state = "on"
            for when, change in self.lab.transitions:
                if when <= t:
                    state = change
            return state == "on"
        local = datetime.fromtimestamp(t, self.lab.site.site.tz)  # before the demo: a schedule ran
        return not (
            (local.weekday() < 5 and local.hour >= 23)
            or (1 <= local.weekday() <= 5 and local.hour < 7)
        )

    async def query(self, promql: str) -> list[Sample]:
        now = self.lab.clock.now().timestamp()
        if promql in self._gpu_fields:
            field = self._gpu_fields[promql]
            return [Sample({"gpu": g["index"]}, float(g[field])) for g in self.lab.gpus()]
        if promql == "demo_decode_tokens_per_second" and self.lab.llm_healthy():
            return [Sample({}, round(58 + 12 * _noise(now, 9), 1))]
        if promql == "demo_host_power_watts" and self.lab.os_up():
            return [Sample({}, self.lab.watts_at(now))]
        if promql == "demo_price_per_kwh":
            point = self.tariff.price_at(self.lab.clock.now())
            return [Sample({"period": point.period or ""}, point.price)]
        return []

    async def query_range(
        self, promql: str, start: datetime, end: datetime, step: int
    ) -> list[Series]:
        times = itertools.takewhile(
            lambda t: t <= end.timestamp(), itertools.count(start.timestamp(), step)
        )
        if promql == "demo_host_power_watts":
            points = [(t, self.lab.watts_at(t)) for t in times if self.on_at(t)]
            return [Series({}, points)]
        if promql == "demo_price_per_kwh":
            by_period: dict[str, list[tuple[float, float]]] = {}
            for t in times:
                point = self.tariff.price_at(datetime.fromtimestamp(t, start.tzinfo))
                by_period.setdefault(point.period or "", []).append((t, point.price))
            return [Series({"period": p}, pts) for p, pts in by_period.items()]
        return []


class DemoSilences:
    def __init__(self, clock: Clock, sets: int = 1) -> None:
        self.clock = clock
        self.sets = sets
        self.silences: dict[str, Silence] = {}
        self._ids = itertools.count(1)

    async def create(self, until: datetime, comment: str) -> list[str]:
        ids = [f"demo-{next(self._ids)}" for _ in range(self.sets)]
        for silence_id in ids:
            self.silences[silence_id] = Silence(silence_id, until, comment)
        return ids

    async def active(self) -> list[Silence]:
        now = self.clock.now()
        return [s for s in self.silences.values() if s.ends_at > now]

    async def expire(self, silence_id: str) -> None:
        self.silences.pop(silence_id, None)


def seed_power_log(log: PowerLog, lab: DemoLab, days: int = 35) -> None:
    """The demo's past: the power changes its made-up schedule would have logged."""
    metrics = DemoMetrics(lab)
    t, end = lab.started_at.timestamp() - days * 86400, lab.started_at.timestamp()
    while t < end:
        log.observe("on" if metrics.on_at(t) else "off", datetime.fromtimestamp(t, UTC))
        t += 300
