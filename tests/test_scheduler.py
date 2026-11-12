"""The scheduler: boundaries, acting once, catch-up, holds and skips, busy hosts, protected
windows, and its state across restarts."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeClock, MakePanel, lab, site_config

from aipanel.config.settings import ScheduleRule
from aipanel.panel import Panel
from aipanel.scheduler import Scheduler, boundaries

pytestmark = pytest.mark.anyio

NIGHTS = {
    "name": "nights",
    "days": ["mon", "tue", "wed", "thu", "fri"],
    "off": "23:00",
    "on": "07:00",
}
WED = datetime(2026, 3, 4, tzinfo=UTC)  # a Wednesday; the demo site runs on UTC


def at(hhmm: str, day: int = 0, seconds: int = 0) -> datetime:
    hours, mins = hhmm.split(":")
    return WED + timedelta(days=day, hours=int(hours), minutes=int(mins), seconds=seconds)


async def enable(panel: Panel, clock: FakeClock, when: datetime, **schedule: object) -> None:
    """Turn the schedule on at `when` and let the scheduler see it."""
    clock.set(when)
    panel.store.update({"schedule": {"enabled": True, "rules": [NIGHTS], **schedule}}, "test")
    await panel.scheduler.tick()


async def tick(panel: Panel, clock: FakeClock, when: datetime) -> None:
    clock.set(when)
    await panel.scheduler.tick()
    await panel.runner.wait()


def jobs(panel: Panel) -> list[tuple[str, str]]:
    return [(j.action, j.actor) for j in reversed(panel.runner.history)]


def result(panel: Panel, kind: str, when: datetime) -> str:
    return panel.scheduler.state.done[f"nights|{kind}|{when.isoformat()}"]


# Boundaries


def test_boundaries_cross_midnight() -> None:
    rule = ScheduleRule(name="fri", days=["fri"], off="23:00", on="07:00")
    found = boundaries([rule], ZoneInfo("UTC"), at("00:00", 2), at("00:00", 5))
    assert [(b.kind, b.at) for b in found] == [("off", at("23:00", 2)), ("on", at("07:00", 3))]


def test_boundaries_within_one_day() -> None:
    rule = ScheduleRule(name="early", days=["thu"], off="01:00", on="05:00")
    found = boundaries([rule], ZoneInfo("UTC"), at("00:00"), at("00:00", 2))
    assert [(b.kind, b.at) for b in found] == [("off", at("01:00", 1)), ("on", at("05:00", 1))]


def test_boundaries_follow_daylight_saving_time() -> None:
    rule = ScheduleRule(name="d", days=["sat", "sun"], off="23:00", on="07:00")
    berlin = ZoneInfo("Europe/Berlin")  # clocks go forward on Sunday 2026-03-29
    start = datetime(2026, 3, 28, tzinfo=UTC)
    offs = [
        b.at
        for b in boundaries([rule], berlin, start, start + timedelta(days=2))
        if b.kind == "off"
    ]
    assert offs == [datetime(2026, 3, 28, 22, tzinfo=UTC), datetime(2026, 3, 29, 21, tzinfo=UTC)]


def test_disabled_rules_have_no_boundaries() -> None:
    rule = ScheduleRule(name="x", enabled=False, days=["wed"], off="23:00", on="07:00")
    assert boundaries([rule], ZoneInfo("UTC"), at("00:00"), at("00:00", 2)) == []


# Acting at boundaries


async def test_off_then_on(panel: Panel, clock: FakeClock) -> None:
    await enable(panel, clock, at("22:50"))
    await tick(panel, clock, at("23:00", seconds=10))
    assert jobs(panel) == [("shutdown", "scheduler")] and lab(panel).power == "off"
    shutdown = panel.runner.history[0]
    until = datetime.fromisoformat(shutdown.params["silence_until"])
    assert until == at("07:20", 1)  # the next on time and the boot grace
    await tick(panel, clock, at("23:00", seconds=40))
    assert len(jobs(panel)) == 1  # each boundary acts once
    await tick(panel, clock, at("07:00", 1, seconds=10))
    assert jobs(panel)[-1] == ("power_on", "scheduler") and lab(panel).llm_healthy()
    assert panel.metrics.get("aipanel_schedule_last_boundary_success") == 1


async def test_a_manual_power_on_inside_an_off_window_is_respected(
    panel: Panel, clock: FakeClock
) -> None:
    await enable(panel, clock, at("22:50"))
    await tick(panel, clock, at("23:00", seconds=10))
    clock.set(at("01:00", 1))
    await panel.runner.start("power_on", "alice")
    await panel.runner.wait()
    for when in (at("01:30", 1), at("03:00", 1), at("06:59", 1)):
        await tick(panel, clock, when)
    assert jobs(panel) == [("shutdown", "scheduler"), ("power_on", "alice")]
    assert lab(panel).power == "on"


async def test_catch_up_after_the_panel_was_down(panel: Panel, clock: FakeClock) -> None:
    await enable(panel, clock, at("22:00"))
    await tick(panel, clock, at("23:20"))  # nothing ran from 22:00 to 23:20
    assert jobs(panel) == [("shutdown", "scheduler")]


async def test_too_late_to_catch_up_is_skipped_and_logged(panel: Panel, clock: FakeClock) -> None:
    await enable(panel, clock, at("22:00"))
    await tick(panel, clock, at("23:45"))
    assert jobs(panel) == [] and result(panel, "off", at("23:00")).startswith("missed")
    assert panel.metrics.get("aipanel_schedule_last_boundary_success") == 0
    assert panel.audit.read(1, "schedule.boundary")[0]["ok"] is False


async def test_a_new_schedule_does_not_act_on_past_boundaries(
    panel: Panel, clock: FakeClock
) -> None:
    await enable(panel, clock, at("23:10"))  # ten minutes after an off time
    await tick(panel, clock, at("23:11"))
    assert jobs(panel) == [] and panel.scheduler.state.done == {}


async def test_a_disabled_schedule_does_nothing(panel: Panel, clock: FakeClock) -> None:
    clock.set(at("22:50"))
    panel.store.update({"schedule": {"rules": [NIGHTS]}}, "test")  # not enabled
    await tick(panel, clock, at("22:50"))
    await tick(panel, clock, at("23:00", seconds=10))
    assert jobs(panel) == []


# Holds and skips


async def test_hold_on(panel: Panel, clock: FakeClock) -> None:
    await enable(panel, clock, at("22:50"))
    panel.scheduler.hold("on", at("08:00", 1), "alice")
    assert [note for b, note in panel.scheduler.upcoming()][:1] == ["held on"]
    await tick(panel, clock, at("23:00", seconds=10))
    assert jobs(panel) == [] and result(panel, "off", at("23:00")).startswith("held on")


async def test_hold_off(panel: Panel, clock: FakeClock) -> None:
    lab(panel).bmc("off")
    await enable(panel, clock, at("06:50", 1))
    panel.scheduler.hold("off", at("09:00", 1), "alice")
    await tick(panel, clock, at("07:00", 1, seconds=10))
    assert jobs(panel) == [] and result(panel, "on", at("07:00", 1)).startswith("held off")
    assert panel.audit.read(1, "schedule.hold")[0]["actor"] == "alice"


async def test_skip_the_next_event(panel: Panel, clock: FakeClock) -> None:
    await enable(panel, clock, at("22:50"))
    first, _ = panel.scheduler.upcoming()[0]
    panel.scheduler.skip(first.id, "alice")
    assert panel.scheduler.upcoming()[0][1] == "skipped"
    await tick(panel, clock, at("23:00", seconds=10))
    assert jobs(panel) == [] and result(panel, "off", at("23:00")) == "skipped, as asked"
    with pytest.raises(ValueError, match="no such"):
        panel.scheduler.skip("nights|off|1999-01-01T00:00:00+00:00", "alice")


# A busy host


async def test_busy_waits_then_skips(panel: Panel, clock: FakeClock) -> None:
    lab(panel).busy = 1
    await enable(panel, clock, at("22:50"))
    await tick(panel, clock, at("23:00", seconds=10))
    pending = panel.scheduler.state.pending
    assert pending is not None and "LLM slot" in pending.reason
    assert pending.next == at("23:05", seconds=10)
    await tick(panel, clock, at("23:03"))  # before the retry time: not even a poll
    await tick(panel, clock, at("23:05", seconds=20))
    assert panel.scheduler.state.pending is not None
    await tick(panel, clock, at("23:31"))
    assert jobs(panel) == [] and panel.scheduler.state.pending is None
    assert result(panel, "off", at("23:00")).startswith("skipped: busy for 30 min")
    assert panel.metrics.get("aipanel_schedule_last_boundary_success") == 0


async def test_busy_then_idle_within_the_grace_time(panel: Panel, clock: FakeClock) -> None:
    lab(panel).busy = 1
    await enable(panel, clock, at("22:50"))
    await tick(panel, clock, at("23:00", seconds=10))
    lab(panel).busy = 0
    await tick(panel, clock, at("23:05", seconds=20))
    assert jobs(panel) == [("shutdown", "scheduler")]


async def test_force_after_the_grace_time(panel: Panel, clock: FakeClock) -> None:
    lab(panel).busy = 1
    lab(panel).tasks = [{"type": "vzdump", "id": "211"}]
    await enable(panel, clock, at("22:50"), force_after_grace=True)
    await tick(panel, clock, at("23:00", seconds=10))
    await tick(panel, clock, at("23:31"))
    assert jobs(panel) == [("shutdown", "scheduler")] and lab(panel).power == "off"
    assert "forced" in result(panel, "off", at("23:00"))


# Protected windows


async def test_a_stop_inside_a_protected_window_is_refused(panel: Panel, clock: FakeClock) -> None:
    await enable(panel, clock, at("22:50"))
    # git adds a protected window over the rule's off time after the rule was saved
    panel.loader.current = site_config(
        protected_windows=[{"days": ["wed"], "from": "22:55", "to": "23:30", "reason": "backup"}]
    )
    assert "overlaps the protected window 'backup'" in (panel.store.conflict() or "")
    await tick(panel, clock, at("23:00", seconds=10))
    assert jobs(panel) == [] and "protected window 'backup'" in result(panel, "off", at("23:00"))


# State


async def test_handled_boundaries_survive_a_restart(
    make_panel: MakePanel, panel: Panel, clock: FakeClock
) -> None:
    await enable(panel, clock, at("22:50"))
    await tick(panel, clock, at("23:00", seconds=10))
    s = panel.scheduler
    again = Scheduler(s.path, s.site, s.settings, s.runner, s.poller, clock, s.audit, s.metrics)
    clock.set(at("23:01"))
    await again.tick()
    await panel.runner.wait()
    assert jobs(panel) == [("shutdown", "scheduler")]
