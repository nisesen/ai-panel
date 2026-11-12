"""Every action's sequence against the demo fakes: success, timeout, refusal; and the runner's
lock, cooldown, confirmation, dry-run and audit."""

from datetime import timedelta

import pytest
from conftest import FakeClock, MakePanel, lab, site_config

from aipanel.actions import REGISTRY
from aipanel.actions.base import Refused
from aipanel.config.site import ActionName
from aipanel.panel import Panel
from aipanel.runner import Job

pytestmark = pytest.mark.anyio
HOST = "gpu-demo"


async def run(panel: Panel, name: str, confirm: str | None = HOST, **params: object) -> Job:
    await panel.runner.start(name, "alice", params, confirm)
    job = await panel.runner.wait()
    assert job is not None
    return job


def steps(job: Job) -> str:
    return "\n".join(s.message for s in job.steps)


def test_every_action_name_is_registered() -> None:
    from typing import get_args

    assert set(REGISTRY) == set(get_args(ActionName))


# Power on


async def test_power_on(panel: Panel, clock: FakeClock) -> None:
    lab(panel).bmc("off")
    made = await panel.backends.silences.create(clock.now() + timedelta(hours=8), "x")  # type: ignore[union-attr]
    job = await run(panel, "power_on")
    assert job.status == "succeeded", steps(job)
    assert lab(panel).power == "on" and lab(panel).llm_healthy()
    assert len(made) == 2 and all(f"end silence {i}" in steps(job) for i in made)
    assert await panel.backends.silences.active() == []  # type: ignore[union-attr]


async def test_power_on_refused_when_on(panel: Panel) -> None:
    with pytest.raises(Refused, match="on already"):
        await panel.runner.start("power_on", "alice")


async def test_power_on_times_out_when_the_host_never_boots(panel: Panel) -> None:
    lab(panel).bmc("off")
    lab(panel).never_boots = True
    job = await run(panel, "power_on")
    assert job.status == "timed_out" and "answer over SSH" in (job.error or "")


async def test_power_on_refused_when_the_bmc_does_not_answer(panel: Panel) -> None:
    lab(panel).bmc("off")
    lab(panel).bmc_down = True
    with pytest.raises(Refused, match="BMC does not answer"):
        await panel.runner.start("power_on", "alice")


# Shutdown


async def test_shutdown(panel: Panel, clock: FakeClock) -> None:
    job = await run(panel, "shutdown", silence_minutes=60)
    assert job.status == "succeeded", steps(job)
    assert lab(panel).power == "off"
    assert "llm-ctl shutdown" in steps(job)
    silences = await panel.backends.silences.active()  # type: ignore[union-attr]
    assert len(silences) == 2  # the demo config has two matcher sets
    assert all(s.ends_at == job.started_at + timedelta(minutes=60) for s in silences)
    assert "(2 silences)" in steps(job)


async def test_shutdown_needs_the_typed_host_name(panel: Panel) -> None:
    for wrong in (None, "", "yes", "GPU-DEMO"):
        with pytest.raises(Refused, match=f"type '{HOST}'"):
            await panel.runner.start("shutdown", "alice", {}, wrong)
    assert lab(panel).power == "on"


async def test_shutdown_refused_while_proxmox_runs_a_task(panel: Panel) -> None:
    lab(panel).tasks = [{"type": "vzdump", "id": "210"}]
    with pytest.raises(Refused, match="vzdump") as e:
        await panel.runner.start("shutdown", "alice", {}, HOST)
    assert e.value.retryable


async def test_shutdown_refused_inside_a_protected_window(panel: Panel, clock: FakeClock) -> None:
    clock.set(clock.now().replace(hour=21, minute=30))  # the demo protects 21:00-22:30
    with pytest.raises(Refused, match="nightly backups"):
        await panel.runner.start("shutdown", "alice", {}, HOST)


async def test_shutdown_times_out_and_then_allows_the_emergency(panel: Panel) -> None:
    lab(panel).never_powers_off = True
    job = await run(panel, "shutdown")
    assert job.status == "timed_out" and "power off" in (job.error or "")
    assert panel.runner.last_failed == "shutdown"
    job = await run(panel, "emergency_soft_off")
    assert job.status == "timed_out"  # the soft off cannot finish on this stuck host either
    panel.runner._last_power = None  # past the cooldown
    job = await run(panel, "emergency_off")
    assert job.status == "succeeded" and lab(panel).power == "off"


async def test_shutdown_refused_when_the_host_does_not_answer(panel: Panel) -> None:
    lab(panel).ssh_down = True
    with pytest.raises(Refused, match="does not answer over SSH"):
        await panel.runner.start("shutdown", "alice", {}, HOST)


# Reboot


async def test_reboot(panel: Panel) -> None:
    job = await run(panel, "reboot")
    assert job.status == "succeeded", steps(job)
    assert lab(panel).boot_count == 2 and lab(panel).llm_healthy()
    assert "the host to go down" in steps(job) and "the host to answer again" in steps(job)
    assert await panel.backends.silences.active() == []  # type: ignore[union-attr]


async def test_reboot_times_out_when_the_host_does_not_come_back(panel: Panel) -> None:
    lab(panel).never_boots = True
    job = await run(panel, "reboot")
    assert job.status == "timed_out" and "answer again" in (job.error or "")


# The LLM


async def test_llm_switch(panel: Panel) -> None:
    job = await run(panel, "llm_switch", confirm=None, model="moe")
    assert job.status == "succeeded", steps(job)
    assert lab(panel).model == "moe"
    calls = lab(panel).calls
    order = [c for c in calls if c.startswith(("client-checks", "llm"))]
    assert order == ["client-checks pause", "llm switch moe", "client-checks resume"]
    assert "serving: demo-moe" in steps(job)


async def test_llm_switch_refusals(panel: Panel) -> None:
    with pytest.raises(Refused, match="unknown model"):
        await panel.runner.start("llm_switch", "alice", {"model": "huge"})
    with pytest.raises(Refused, match="bad parameters"):
        await panel.runner.start("llm_switch", "alice", {"model": "x; reboot"})
    await panel.poller.refresh()
    with pytest.raises(Refused, match="serving already"):
        await panel.runner.start("llm_switch", "alice", {"model": "dense"})


async def test_llm_switch_times_out_and_still_resumes_the_checks(panel: Panel) -> None:
    lab(panel).llm_never_ready = True
    job = await run(panel, "llm_switch", confirm=None, model="moe")
    assert job.status == "timed_out"
    order = [c for c in lab(panel).calls if c.startswith(("client-checks", "llm"))]
    assert order[-1] == "client-checks resume" and not lab(panel).checks_paused


async def test_llm_restart_start_stop(panel: Panel) -> None:
    assert (await run(panel, "llm_restart", confirm=None)).status == "succeeded"
    with pytest.raises(Refused, match="needs confirmation"):
        await panel.runner.start("llm_stop", "alice")
    job = await run(panel, "llm_stop", confirm="yes")
    assert job.status == "succeeded" and lab(panel).model is None
    job = await run(panel, "llm_start", confirm=None)
    assert job.status == "succeeded" and lab(panel).llm_healthy()


async def test_agent_error_fails_the_action(panel: Panel) -> None:
    lab(panel).refuse = {"llm"}
    job = await run(panel, "llm_restart", confirm=None)
    assert job.status == "failed" and "llm failed" in (job.error or "")


# Guests and maintenance


async def test_guest_shutdown_and_start(panel: Panel) -> None:
    job = await run(panel, "guest_shutdown", confirm="yes", guest=211)
    assert job.status == "succeeded" and lab(panel).guest_status(211) == "stopped"
    job = await run(panel, "guest_start", confirm="yes", guest=211)
    assert job.status == "succeeded" and lab(panel).guest_status(211) == "running"


async def test_guest_refusals(panel: Panel) -> None:
    with pytest.raises(Refused, match="not in the site config"):
        await panel.runner.start("guest_reboot", "alice", {"guest": 999}, "yes")
    with pytest.raises(Refused, match="needs confirmation"):
        await panel.runner.start("guest_reboot", "alice", {"guest": 211})


async def test_probe_and_client_checks(panel: Panel) -> None:
    job = await run(panel, "probe", confirm=None)
    assert job.status == "succeeded" and "gpus:" in steps(job)
    job = await run(panel, "client_checks", confirm=None, op="pause")
    assert job.status == "succeeded" and lab(panel).checks_paused


# Emergency


async def test_emergency_refused_while_the_host_answers(panel: Panel) -> None:
    with pytest.raises(Refused, match="still answers"):
        await panel.runner.start("emergency_off", "alice", {}, HOST)


async def test_emergency_reset_for_a_hung_host(panel: Panel) -> None:
    lab(panel).ssh_down = True
    job = await run(panel, "emergency_reset")
    assert job.status == "succeeded" and "bmc reset" in lab(panel).calls


# The runner


async def test_one_action_at_a_time(panel: Panel) -> None:
    await panel.runner.start("shutdown", "alice", {}, HOST)
    with pytest.raises(Refused, match="Shut down is running") as e:
        await panel.runner.start("probe", "alice")
    assert e.value.retryable
    await panel.runner.wait()


async def test_cooldown_between_power_actions(panel: Panel, clock: FakeClock) -> None:
    await run(panel, "shutdown")
    with pytest.raises(Refused, match="wait"):
        await panel.runner.start("power_on", "alice")
    clock.advance(seconds=60)
    assert (await run(panel, "power_on")).status == "succeeded"


async def test_disabled_action_does_not_exist(make_panel: MakePanel) -> None:
    panel = make_panel(site_config(**{"actions.enabled": ["probe"]}))
    with pytest.raises(Refused, match="no action"):
        await panel.runner.start("shutdown", "alice", {}, HOST)
    assert (await run(panel, "probe", confirm=None)).status == "succeeded"


async def test_dry_run_reads_but_changes_nothing(make_panel: MakePanel) -> None:
    panel = make_panel(site_config(**{"actions.dry_run": True}))
    job = await run(panel, "shutdown")
    assert job.status == "succeeded" and job.dry_run
    assert "DRY-RUN, not done: llm-ctl shutdown" in steps(job)
    assert lab(panel).power == "on" and "shutdown" not in lab(panel).calls
    assert await panel.backends.silences.active() == []  # type: ignore[union-attr]
    job = await run(panel, "power_on", confirm=None) if lab(panel).power == "off" else job
    switch = await run(panel, "llm_switch", confirm=None, model="moe")
    assert switch.status == "succeeded" and lab(panel).model == "dense"


async def test_actions_are_audited_and_measured(panel: Panel) -> None:
    await run(panel, "probe", confirm=None)
    with pytest.raises(Refused):
        await panel.runner.start("power_on", "alice")
    events = [e["event"] for e in panel.audit.read(10)]
    assert events == ["action.refused", "action.finished", "action.started"]
    finished = panel.audit.read(10, "action.finished")[0]
    assert finished["actor"] == "alice" and finished["status"] == "succeeded" and finished["steps"]
    labels = {"action": "probe", "actor": "alice"}
    assert panel.metrics.get("aipanel_action_last_success", labels) == 1
