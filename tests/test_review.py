"""Regression tests for the v0.1.0 review (SEC security, COR correctness, OPS operations): each
reproduces a reported failure and checks the fixed behaviour."""

import asyncio
import os
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeClock, MakePanel, lab
from pydantic import ValidationError

from aipanel import files, yamlio
from aipanel.audit import MAX_TEXT, Audit
from aipanel.auth import CSRF_WINDOW_SECONDS, User, csrf_ok, csrf_token
from aipanel.backends.energy import PrometheusPower, PrometheusPrice
from aipanel.backends.process import run as run_process
from aipanel.backends.protocols import BackendError, PricePoint, Sample, Series, Unreachable
from aipanel.cli import parser, server_config
from aipanel.config.settings import Settings
from aipanel.config.site import AuthConfig
from aipanel.cost import integrate
from aipanel.panel import Panel
from aipanel.web.routes import local_path

pytestmark = pytest.mark.anyio

NIGHTS = {
    "name": "nights",
    "days": ["mon", "tue", "wed", "thu", "fri"],
    "off": "23:00",
    "on": "07:00",
}
WED = datetime(2026, 3, 4, tzinfo=UTC)
HOST = "gpu-demo"


def at(hhmm: str, day: int = 0, seconds: int = 0) -> datetime:
    h, m = hhmm.split(":")
    return WED + timedelta(days=day, hours=int(h), minutes=int(m), seconds=seconds)


async def enable(panel: Panel, clock: FakeClock, when: datetime, **schedule: object) -> None:
    clock.set(when)
    panel.store.update({"schedule": {"enabled": True, "rules": [NIGHTS], **schedule}}, "test")
    await panel.scheduler.tick()


async def tick(panel: Panel, clock: FakeClock, when: datetime) -> None:
    clock.set(when)
    await panel.scheduler.tick()
    await panel.runner.wait()


def jobs(panel: Panel) -> list[tuple[str, str]]:
    return [(j.action, j.actor) for j in reversed(panel.runner.history)]


async def silences(panel: Panel) -> list[datetime]:
    assert panel.backends.silences is not None
    return [s.ends_at for s in await panel.backends.silences.active()]


# OPS-1: no stop without the BMC, the only way back on


async def test_ops1_no_scheduled_stop_while_the_bmc_does_not_answer(
    panel: Panel, clock: FakeClock
) -> None:
    lab(panel).bmc_down = True
    await enable(panel, clock, at("22:50"))
    await tick(panel, clock, at("23:00", seconds=10))
    assert jobs(panel) == [] and lab(panel).os_up()
    assert panel.scheduler.state.pending is not None  # tried again within the grace time
    await tick(panel, clock, at("23:31"))
    assert (
        lab(panel).os_up()
        and "BMC" in panel.scheduler.state.done[f"nights|off|{at('23:00').isoformat()}"]
    )


async def test_ops1_no_manual_stop_with_a_stale_bmc_reading(panel: Panel, clock: FakeClock) -> None:
    lab(panel).bmc_down = True
    for name in ("shutdown", "reboot"):
        with pytest.raises(Exception, match="BMC has not answered"):
            await panel.runner.start(name, "alice", {}, HOST)


# OPS-2, COR-4: busy fails closed


async def test_cor4_unreadable_slots_count_as_busy(panel: Panel, clock: FakeClock) -> None:
    lab(panel).busy = 2

    async def slots() -> tuple[int, int] | None:
        raise Unreachable("LLM: ReadTimeout")

    panel.backends.llm.slots = slots  # type: ignore[method-assign]
    await enable(panel, clock, at("22:50"))
    await tick(panel, clock, at("23:00", seconds=10))
    assert jobs(panel) == [] and "unknown" in (panel.scheduler.state.pending or object()).reason  # type: ignore[attr-defined]


async def test_ops2_prometheus_down_does_not_hide_busy_slots(
    panel: Panel, clock: FakeClock
) -> None:
    lab(panel).busy = 2

    async def down(promql: str) -> list[Sample]:
        raise Unreachable("Prometheus: ConnectError")

    panel.backends.metrics.query = down  # type: ignore[union-attr,method-assign]
    await enable(panel, clock, at("22:50"))
    await tick(panel, clock, at("23:00", seconds=10))
    snap = panel.poller.snapshot
    assert snap.llm.busy == 2 and "decode" in snap.errors and jobs(panel) == []


async def test_a_stopped_llm_is_not_busy(panel: Panel, clock: FakeClock) -> None:
    lab(panel).model = None  # not serving: nothing to interrupt
    await enable(panel, clock, at("22:50"))
    await tick(panel, clock, at("23:00", seconds=10))
    assert jobs(panel) == [("shutdown", "scheduler")]


# COR-3, OPS-3: a failed stop ends its own silences; the boundary is marked by the outcome


async def test_cor3_failed_shutdown_ends_its_silences_and_fails_the_boundary(
    panel: Panel, clock: FakeClock
) -> None:
    await enable(panel, clock, at("22:50"))
    lab(panel).refuse = {"shutdown"}
    await tick(panel, clock, at("23:00", seconds=10))
    assert panel.runner.history[0].status == "failed" and lab(panel).os_up()
    assert await silences(panel) == []
    await tick(panel, clock, at("23:00", seconds=40))
    assert panel.metrics.get("aipanel_schedule_last_boundary_success") == 0
    assert panel.scheduler.state.last and "shutdown failed" in panel.scheduler.state.last.result


async def test_ops3_a_good_stop_marks_success_when_it_ends(panel: Panel, clock: FakeClock) -> None:
    await enable(panel, clock, at("22:50"))
    await tick(panel, clock, at("23:00", seconds=10))
    assert panel.scheduler.state.running is not None or panel.scheduler.state.last is None
    await tick(panel, clock, at("23:00", seconds=40))
    assert panel.scheduler.state.last and panel.scheduler.state.last.result == "shutdown succeeded"


# OPS-4: a dead BMC is shown as dead


async def test_ops4_a_dead_bmc_stays_visible(panel: Panel, clock: FakeClock) -> None:
    await panel.poller.refresh()
    lab(panel).bmc_down = True
    seen = []
    for _ in range(20):  # the background loop: every 15 s, the BMC every 60 s while it answers
        await panel.poller.refresh(bmc=False)
        panel.update_status_metrics()
        seen.append(panel.metrics.get("aipanel_backend_up", {"backend": "bmc"}))
        clock.advance(seconds=15)
    first = seen.index(0.0)
    assert first <= 4 and set(seen[first:]) == {0.0}  # found within a minute, then kept
    assert panel.poller.snapshot.power == "unknown" and panel.poller.snapshot.host is not None
    assert panel.metrics.get("aipanel_bmc_last_success_timestamp") is not None


# COR-1: silences follow the schedule's next on while the host is off


async def test_cor1_skipping_the_next_on_extends_the_silences(
    panel: Panel, clock: FakeClock
) -> None:
    await enable(panel, clock, at("22:50"))
    await tick(panel, clock, at("23:00", seconds=10))
    clock.set(at("06:00", 1))
    on, _ = next((b, n) for b, n in panel.scheduler.upcoming() if b.kind == "on")
    panel.scheduler.skip(on.id, "alice")
    await tick(panel, clock, at("07:00", 1, seconds=10))
    clock.set(at("07:21", 1))
    assert lab(panel).power == "off" and await silences(panel)


async def test_cor1_hold_off_extends_the_silences(panel: Panel, clock: FakeClock) -> None:
    await enable(panel, clock, at("22:50"))
    await tick(panel, clock, at("23:00", seconds=10))
    clock.set(at("06:00", 1))
    panel.scheduler.hold("off", at("12:00", 2), "alice")
    await tick(panel, clock, at("07:00", 1, seconds=10))
    clock.set(at("07:21", 1))
    assert await silences(panel)


def test_cor1b_no_next_on_when_the_schedule_is_off(panel: Panel, clock: FakeClock) -> None:
    clock.set(at("12:00"))
    panel.store.update({"schedule": {"enabled": False, "rules": [NIGHTS]}}, "test")
    assert panel.scheduler.next_on(clock.now()) is None


async def test_cor1c_a_long_hold_off_silences_until_it_ends(panel: Panel, clock: FakeClock) -> None:
    await enable(panel, clock, at("22:50"))
    panel.scheduler.hold("off", at("12:00", 20), "alice")
    await tick(panel, clock, at("23:00", seconds=10))
    clock.set(at("12:00", 19))
    assert await silences(panel)


# COR-2: a blip at a boundary is tried again


async def test_cor2_bmc_blip_at_the_on_boundary(panel: Panel, clock: FakeClock) -> None:
    await enable(panel, clock, at("22:50"))
    await tick(panel, clock, at("23:00", seconds=10))
    lab(panel).bmc_down = True
    await tick(panel, clock, at("07:00", 1, seconds=10))
    lab(panel).bmc_down = False
    clock.advance(seconds=60)  # the cooldown between power actions
    await tick(panel, clock, at("07:05", 1))
    assert lab(panel).power == "on"


async def test_cor2_ssh_blip_at_the_off_boundary(panel: Panel, clock: FakeClock) -> None:
    await enable(panel, clock, at("22:50"))
    lab(panel).ssh_down = True
    await tick(panel, clock, at("23:00", seconds=10))
    lab(panel).ssh_down = False
    await tick(panel, clock, at("23:05", seconds=20))
    assert lab(panel).power == "off"


# COR-5, OPS-12: a cancelled command leaves no child; a broken backend stops nothing else


async def test_cor5_a_cancelled_command_is_killed(tmp_path: Path) -> None:
    marker = tmp_path / "pid"
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            run_process(["sh", "-c", f"echo $$ > {marker}; exec sleep 30"], timeout=30), 0.5
        )
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


async def test_ops12_an_unexpected_error_in_one_part_stops_nothing(
    panel: Panel, clock: FakeClock
) -> None:
    async def garbled() -> list[object]:
        raise AttributeError("'str' object has no attribute 'get'")

    panel.backends.silences.active = garbled  # type: ignore[union-attr,method-assign]
    await enable(panel, clock, at("22:50"))
    await tick(panel, clock, at("23:00", seconds=10))
    assert "silences" in panel.poller.snapshot.errors and lab(panel).power == "off"


# COR-6, COR-14: cost


async def test_cor6_a_prometheus_gap_is_neither_off_nor_savings() -> None:
    class Gappy:
        async def query(self, promql: str) -> list[Sample]:
            return []

        async def query_range(
            self, promql: str, start: datetime, end: datetime, step: int
        ) -> list[Series]:
            t0 = start.timestamp()
            return [Series({}, [(t0 + i * 300, 400.0) for i in range(12) if not 3 <= i < 9])]

    start = datetime(2026, 3, 4, 12, tzinfo=UTC)
    grid = await PrometheusPower(Gappy(), "q").history(start, start + timedelta(hours=1), 300)
    total = integrate(
        range(int(start.timestamp()), int(start.timestamp()) + 3600, 300),
        grid,
        {t: PricePoint(0.4) for t in grid},
        300.0,
        15.0,
    )
    assert total.hours_off == 0 and total.saved == 0 and total.hours_on == pytest.approx(0.5)


async def test_cor14_last_week_is_local_time_across_dst() -> None:
    zone = ZoneInfo("Europe/Amsterdam")  # clocks go forward on 2026-03-29
    now = datetime(2026, 3, 30, 12, tzinfo=zone).astimezone(UTC)

    class Hourly:
        async def query(self, promql: str) -> list[Sample]:
            return []

        async def query_range(
            self, promql: str, start: datetime, end: datetime, step: int
        ) -> list[Series]:
            t, points = start.timestamp(), []
            while t <= end.timestamp():
                points.append((t, float(datetime.fromtimestamp(t, zone).hour)))
                t += step
            return [Series({}, points)]

    price = PrometheusPrice(Hourly(), "q", None, "EUR", zone, lambda: now)
    nine = datetime(2026, 4, 2, 9, tzinfo=zone)  # after the change; a week earlier was before
    future = await price.future(
        nine.astimezone(UTC), nine.astimezone(UTC) + timedelta(hours=1), 3600
    )
    assert future[int(nine.timestamp())].price == 9.0  # the same wall-clock hour


# COR-7: the emergency gate survives other actions and restarts


async def test_cor7_a_probe_after_a_failed_shutdown_keeps_the_emergency_open(
    make_panel: MakePanel, panel: Panel
) -> None:
    lab(panel).refuse = {"shutdown"}
    await panel.runner.start("shutdown", "alice", {"silence_minutes": 0}, HOST)
    assert (await panel.runner.wait()).status == "failed"  # type: ignore[union-attr]
    await panel.runner.start("probe", "alice")
    await panel.runner.wait()
    assert panel.runner.last_failed == "shutdown"
    restarted = make_panel(state=panel.state_dir)
    assert restarted.runner.last_failed == "shutdown"


# COR-8: silence bounds


async def test_cor8_silence_until_must_be_aware_and_bounded(panel: Panel) -> None:
    with pytest.raises(Exception, match="bad parameters"):
        await panel.runner.start(
            "shutdown", "alice", {"silence_until": "2026-03-05T07:00:00"}, HOST
        )
    with pytest.raises(Exception, match="7 days at most"):
        await panel.runner.start(
            "shutdown", "alice", {"silence_until": "2099-01-01T00:00:00+00:00"}, HOST
        )


# COR-9, COR-13: catch-up of at least a minute; a pending boundary can be skipped


async def test_cor9_catch_up_has_a_minimum() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"schedule": {"catch_up_minutes": 0}})


async def test_cor13_a_pending_boundary_can_be_skipped(panel: Panel, clock: FakeClock) -> None:
    lab(panel).busy = 1
    await enable(panel, clock, at("22:50"))
    await tick(panel, clock, at("23:00", seconds=10))
    pending = panel.scheduler.state.pending
    assert pending is not None
    panel.scheduler.skip(pending.id, "alice")
    await tick(panel, clock, at("23:05", seconds=20))
    assert panel.scheduler.state.pending is None and jobs(panel) == []
    assert panel.scheduler.state.done[pending.id] == "skipped, as asked"


# COR-11, COR-12, OPS-5: client checks and Alertmanager failures


async def test_cor11_llm_stop_leaves_the_checks_paused(panel: Panel) -> None:
    await panel.runner.start("llm_stop", "alice", confirmation="yes")
    assert (await panel.runner.wait()).status == "succeeded"  # type: ignore[union-attr]
    assert lab(panel).checks_paused
    await panel.runner.start("llm_start", "alice")
    await panel.runner.wait()
    assert not lab(panel).checks_paused


async def test_cor11_a_checks_error_never_fails_the_action(panel: Panel) -> None:
    lab(panel).refuse = {"client-checks"}
    await panel.runner.start("llm_restart", "alice")
    job = await panel.runner.wait()
    assert job is not None and job.status == "succeeded"
    assert any("warning: client checks not paused" in s.message for s in job.steps)


async def test_cor12_alertmanager_error_after_a_good_power_on(panel: Panel) -> None:
    lab(panel).bmc("off")
    assert panel.backends.silences is not None

    async def broken(silence_id: str) -> None:
        raise BackendError("Alertmanager: HTTP 503")

    await panel.backends.silences.create(datetime(2026, 3, 5, tzinfo=UTC), "x")
    panel.backends.silences.expire = broken  # type: ignore[method-assign]
    await panel.runner.start("power_on", "scheduler")
    job = await panel.runner.wait()
    assert job is not None and job.status == "succeeded"


async def test_ops5_an_emergency_goes_on_without_alertmanager(panel: Panel) -> None:
    lab(panel).ssh_down = True

    async def down(until: datetime, comment: str) -> list[str]:
        raise Unreachable("Alertmanager: ConnectError")

    panel.backends.silences.create = down  # type: ignore[union-attr,method-assign]
    await panel.runner.start("emergency_off", "alice", confirmation=HOST)
    job = await panel.runner.wait()
    assert job is not None and job.status == "succeeded" and lab(panel).power == "off"


# OPS-6, OPS-7, OPS-11, OPS-12: state


async def test_ops11_failure_metrics_survive_a_restart(
    make_panel: MakePanel, panel: Panel, clock: FakeClock
) -> None:
    lab(panel).busy = 1
    await enable(panel, clock, at("22:50"))
    for when in (at("23:00", seconds=10), at("23:31")):
        await tick(panel, clock, when)
    await panel.runner.start("probe", "alice")
    await panel.runner.wait()
    restarted = make_panel(state=panel.state_dir)
    assert restarted.metrics.get("aipanel_schedule_last_boundary_success") == 0
    assert (
        restarted.metrics.get("aipanel_action_last_success", {"action": "probe", "actor": "alice"})
        == 1
    )


def test_ops6_a_slow_state_directory_does_not_block_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = Audit(tmp_path / "audit.jsonl")
    real = files.append_line

    def slow(path: Path, line: str) -> None:
        time.sleep(0.5)
        real(path, line)

    monkeypatch.setattr(files, "append_line", slow)
    started = time.monotonic()
    audit.record("test.event", "alice")
    assert time.monotonic() - started < 0.2
    assert audit.read(1)[0]["event"] == "test.event"  # read waits for the queued writes


def test_ops7_an_unreadable_state_directory_runs_read_only(
    make_panel: MakePanel, tmp_path: Path
) -> None:
    state = tmp_path / "locked"
    state.mkdir()
    state.chmod(0)
    try:
        panel = make_panel(state=state)
        assert not panel.state_writable and panel.store.get() == Settings()
    finally:
        state.chmod(0o700)


async def test_ops12_state_files_are_group_readable(panel: Panel) -> None:
    panel.store.update({"poll.interval_seconds": 20}, "alice")
    panel.audit.record("x", "alice")
    files.flush()
    for name in ("settings.json", "audit.jsonl"):
        assert stat.S_IMODE((panel.state_dir / name).stat().st_mode) == 0o640
    assert stat.S_IMODE((panel.state_dir / "csrf.key").stat().st_mode) == 0o600


# SEC-1, SEC-2, SEC-9b: trusted headers need a trusted proxy


def test_sec1_trusted_header_needs_trusted_proxies() -> None:
    base = {"mode": "trusted_header", "allowed_groups": ["admins"]}
    with pytest.raises(ValidationError, match="trusted_proxies"):
        AuthConfig.model_validate(base)
    with pytest.raises(ValidationError, match="not an address or CIDR"):
        AuthConfig.model_validate({**base, "trusted_proxies": ["proxy.example.com"]})
    assert AuthConfig.model_validate({**base, "trusted_proxies": ["0.0.0.0/0", "::/0"]})


def test_sec2_uvicorn_ignores_forwarded_headers() -> None:
    config = server_config(object(), "127.0.0.1", 8080)
    assert config.proxy_headers is False and config.server_header is False


def test_default_listen_is_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIPANEL_LISTEN", raising=False)
    assert parser().parse_args(["serve"]).listen == "127.0.0.1"


# SEC-3: input limits


def test_sec3_yaml_aliases_are_refused() -> None:
    with pytest.raises(ValueError, match="anchors and aliases"):
        yamlio.load("x: &s mon\nschedule: {rules: [{days: [*s, *s]}]}\n")


def test_sec3_settings_list_limits() -> None:
    rule = {"name": "r", "days": ["mon"] * 8, "off": "23:00", "on": "07:00"}
    with pytest.raises(ValidationError):
        Settings.model_validate({"schedule": {"rules": [rule]}})
    many = [{**rule, "name": f"r{i}", "days": ["mon"]} for i in range(51)]
    with pytest.raises(ValidationError):
        Settings.model_validate({"schedule": {"rules": many}})


# SEC-5: audit


def test_sec5_audit_cuts_long_values_and_reads_the_tail(tmp_path: Path) -> None:
    audit = Audit(tmp_path / "audit.jsonl")
    entry = audit.record("action.refused", "mallory", action="A" * 10_000)
    assert len(entry["action"]) == MAX_TEXT + 1
    files.flush()
    (tmp_path / "audit.jsonl").write_text('{"partial\n' * 3)  # junk is skipped
    audit.record("ok", "alice")
    assert [e["event"] for e in audit.read(10)] == ["ok"]


# SEC-7, SEC-9a: CSRF tokens


def test_sec7_tokens_expire_after_two_windows() -> None:
    key, user, now = b"k" * 32, User("alice"), 1_700_000_000.0
    token = csrf_token(key, user, now)
    assert csrf_ok(key, user, token, now + CSRF_WINDOW_SECONDS)
    assert not csrf_ok(key, user, token, now + 2 * CSRF_WINDOW_SECONDS)
    assert not csrf_ok(key, User("bob"), token, now)


def test_sec9a_a_non_ascii_token_is_refused_not_an_error() -> None:
    assert not csrf_ok(b"k" * 32, User("alice"), "ü" * 64)


# SEC-8: redirects stay on the site


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("/", True),
        ("/schedule", True),
        ("//evil.example", False),
        ("/\\evil.example", False),
        ("https://evil.example", False),
        ("", False),
    ],
)
def test_sec8_redirects_stay_on_the_site(value: str, ok: bool) -> None:
    assert (local_path(value, "/x") == value) is ok


async def test_sec3_large_request_bodies_are_refused(panel: Panel) -> None:
    import httpx

    from aipanel.app import MAX_BODY, create_app

    app = create_app(panel, run_background=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://panel") as client:
        big = await client.post("/settings/import", content=b"x" * (MAX_BODY + 1))
        small = await client.post("/settings/import", content=b"x")
    assert big.status_code == 413 and small.status_code == 403  # the CSRF check comes next
