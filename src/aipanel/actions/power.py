"""Power: on through the BMC; off and reboot through the host agent (a clean stop); and the
emergency BMC commands for a host that no longer answers or a shutdown that is stuck.

A planned stop never uses the BMC to cut power: on some hardware a BMC power cycle hard-kills
the guests and does not clear latched faults. The BMC only switches the host on, and confirms
that it went off; and because it is the only way back on, no stop starts while it does not
answer.
"""

from datetime import datetime, timedelta
from typing import ClassVar, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from aipanel.actions.base import (
    Action,
    ActionContext,
    Confirm,
    Refused,
    require_bmc_answering,
    require_host_up,
    require_idle_proxmox,
    require_outside_protected,
)
from aipanel.actions.registry import register
from aipanel.backends.protocols import BackendError, PowerState, Timeout, Unreachable
from aipanel.config.settings import Settings

MAX_SILENCE = timedelta(days=7)


class ShutdownParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    silence_minutes: int | None = Field(None, ge=0, le=10080)
    silence_until: AwareDatetime | None = None
    until_next_on: bool = Field(False, description="Silence until the schedule's next on")


async def power_is(ctx: ActionContext, state: PowerState) -> bool:
    return await ctx.backends.power.status() == state


async def answers(ctx: ActionContext) -> bool:
    await ctx.host_status()  # raises while the host does not answer
    return True


async def still_up(ctx: ActionContext) -> bool:
    """The host is on and answers: a stop that failed left it running."""
    try:
        return await power_is(ctx, "on") and await answers(ctx)
    except BackendError:
        return False


async def boot_id(ctx: ActionContext) -> str | None:
    try:
        return str((await ctx.host_status()).get("host", {}).get("boot_id", ""))
    except BackendError:
        return None


async def guest_running(ctx: ActionContext, guest_id: int) -> bool:
    guests = (await ctx.host_status()).get("guests") or []
    return any(g.get("id") == guest_id and g.get("status") == "running" for g in guests)


async def wait_until_serving(ctx: ActionContext, timeout: float) -> None:
    """After a boot: the LLM's guest runs, and the LLM reports healthy."""
    llm_guest = ctx.site.host.llm_guest
    if llm_guest is not None:
        await ctx.wait_for(
            f"guest {llm_guest} to run", lambda: guest_running(ctx, llm_guest), timeout, 10
        )
    await ctx.wait_for(
        "the LLM to report healthy",
        ctx.backends.llm.health,
        ctx.settings.timeouts.llm_ready_seconds,
        10,
    )


async def stop_agent(ctx: ActionContext, verb: str) -> None:
    """Ask llm-ctl to stop or reboot the host. The host may close the connection as it goes
    down; a timeout is different: llm-ctl did not finish, so the host's real state decides."""
    try:
        await ctx.agent(verb)
    except Timeout as e:
        if await still_up(ctx):
            raise BackendError(
                f"llm-ctl {verb} did not finish ({e}); the host is still up"
            ) from None
        ctx.log(f"llm-ctl {verb} did not answer ({e}); the host no longer does either")
    except Unreachable as e:
        ctx.log(f"the connection closed ({e}); checking the power")


class Stop(Action):
    """Shutdown and reboot: the same preconditions, and the same clean-up after a failure."""

    power = True
    confirm = Confirm.TYPED

    async def check(self, ctx: ActionContext) -> None:
        require_host_up(ctx)
        require_idle_proxmox(ctx)
        require_outside_protected(ctx)
        require_bmc_answering(ctx)

    async def recover(self, ctx: ActionContext) -> None:
        if ctx.silenced and not ctx.dry_run and await still_up(ctx):
            ctx.log("the host is still up: ending the silences of this action")
            await ctx.unsilence(ctx.silenced)


@register
class PowerOn(Action):
    name = "power_on"
    label = "Power on"
    group = "power"
    confirm = Confirm.CLICK
    power = True

    def limit(self, s: Settings) -> float:
        return 2 * s.timeouts.boot_seconds + s.timeouts.llm_ready_seconds + 120

    async def check(self, ctx: ActionContext) -> None:
        if ctx.snapshot.power == "on":
            raise Refused("the BMC reports the host is on already")
        if ctx.snapshot.power == "unknown":
            raise Refused("the BMC does not answer", retryable=True)

    async def run(self, ctx: ActionContext) -> None:
        boot = ctx.settings.timeouts.boot_seconds
        await ctx.do("BMC: power on", ctx.backends.power.on)
        await ctx.wait_for("the host to answer over SSH", lambda: answers(ctx), boot, 10)
        await wait_until_serving(ctx, boot)
        await ctx.unsilence()


@register
class Shutdown(Stop):
    name = "shutdown"
    label = "Shut down"
    group = "power"
    params = ShutdownParams
    warning = "Stops the LLM's guest, then the host with every guest on it."

    def limit(self, s: Settings) -> float:
        return s.timeouts.command_seconds + s.timeouts.shutdown_seconds + 120

    def silence_until(self, ctx: ActionContext) -> datetime:
        params: ShutdownParams = ctx.params
        now = ctx.clock.now()
        if params.until_next_on and ctx.next_on is not None:
            return ctx.next_on + timedelta(minutes=ctx.settings.schedule.boot_grace_minutes)
        if params.silence_until is not None:
            return params.silence_until
        minutes = params.silence_minutes
        if minutes is None:
            minutes = ctx.settings.silences.shutdown_minutes
        return now + timedelta(minutes=minutes)

    async def check(self, ctx: ActionContext) -> None:
        await super().check(ctx)
        given = ctx.params.silence_until
        if given is not None:  # a time from the API: up to 7 days, or the schedule's next on
            limit = ctx.clock.now() + MAX_SILENCE
            if ctx.next_on is not None:
                grace = timedelta(minutes=ctx.settings.schedule.boot_grace_minutes)
                limit = max(limit, ctx.next_on + grace)
            if given > limit:
                raise Refused("a silence may last 7 days at most, or until the next scheduled on")

    async def run(self, ctx: ActionContext) -> None:
        until = self.silence_until(ctx)
        if until > ctx.clock.now():
            await ctx.silence(until, "planned shutdown")
        await stop_agent(ctx, "shutdown")
        await ctx.wait_for(
            "the BMC to report power off",
            lambda: power_is(ctx, "off"),
            ctx.settings.timeouts.shutdown_seconds,
            10,
        )


@register
class Reboot(Stop):
    name = "reboot"
    label = "Reboot"
    group = "power"
    warning = "Restarts the host: every guest on it stops and starts again."

    def limit(self, s: Settings) -> float:
        t = s.timeouts
        return t.command_seconds + t.shutdown_seconds + 2 * t.boot_seconds + t.llm_ready_seconds

    async def run(self, ctx: ActionContext) -> None:
        t = ctx.settings.timeouts
        before = await boot_id(ctx)
        await ctx.silence(
            ctx.clock.now() + timedelta(minutes=ctx.settings.silences.reboot_minutes), "reboot"
        )
        await stop_agent(ctx, "reboot")

        async def gone() -> bool:
            return await boot_id(ctx) != before

        async def back() -> bool:
            now = await boot_id(ctx)
            return now is not None and now != before

        await ctx.wait_for("the host to go down", gone, t.shutdown_seconds, 5)
        await ctx.wait_for("the host to answer again", back, t.boot_seconds, 10)
        await wait_until_serving(ctx, t.boot_seconds)
        await ctx.unsilence(ctx.silenced)

    async def recover(self, ctx: ActionContext) -> None:
        if await boot_id(ctx) is not None:  # back up, or never down: the silences are done
            await ctx.unsilence(ctx.silenced)


class Emergency(Action):
    group = "emergency"
    confirm = Confirm.TYPED
    power = True
    command: ClassVar[Literal["soft", "off", "reset"]]

    def limit(self, s: Settings) -> float:
        return s.timeouts.shutdown_seconds + 120

    async def check(self, ctx: ActionContext) -> None:
        if ctx.snapshot.power == "off":
            raise Refused("the host is off already")
        if ctx.snapshot.power == "unknown":
            raise Refused("the BMC does not answer")
        stuck = ctx.last_failed in ("shutdown", "reboot")
        if ctx.snapshot.host is not None and not stuck:
            raise Refused("the host still answers over SSH: use Shut down or Reboot instead")

    async def run(self, ctx: ActionContext) -> None:
        minutes = ctx.settings.silences.emergency_minutes
        try:
            await ctx.silence(ctx.clock.now() + timedelta(minutes=minutes), self.command)
        except Exception as e:  # an emergency does not wait for Alertmanager
            ctx.log(f"warning: alerts not silenced ({e}); going on")
        power = ctx.backends.power
        command = {"soft": power.soft, "off": power.off, "reset": power.reset}[self.command]
        await ctx.do(f"BMC: chassis power {self.command}", command)
        if self.command == "reset":
            await ctx.wait_for("the BMC to report power on", lambda: power_is(ctx, "on"), 60, 5)
            ctx.log("the host is restarting; watch it come back on the dashboard")
            return
        timeout = ctx.settings.timeouts.shutdown_seconds if self.command == "soft" else 60
        await ctx.wait_for("the BMC to report power off", lambda: power_is(ctx, "off"), timeout, 5)


@register
class EmergencySoftOff(Emergency):
    name = "emergency_soft_off"
    label = "Soft off (ACPI)"
    command = "soft"


@register
class EmergencyOff(Emergency):
    name = "emergency_off"
    label = "Power off now"
    command = "off"


@register
class EmergencyReset(Emergency):
    name = "emergency_reset"
    label = "Reset"
    command = "reset"
