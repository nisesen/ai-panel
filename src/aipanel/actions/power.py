"""Power: on through the BMC; off and reboot through the host agent (a clean stop); and the
emergency BMC commands for a host that no longer answers or a shutdown that is stuck.

A planned stop never uses the BMC to cut power: on some hardware a BMC power cycle hard-kills
the guests and does not clear latched faults. The BMC only switches the host on, and confirms
that it went off.
"""

from datetime import datetime, timedelta
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from aipanel.actions.base import (
    Action,
    ActionContext,
    Confirm,
    Refused,
    require_host_up,
    require_idle_proxmox,
    require_outside_protected,
)
from aipanel.actions.registry import register
from aipanel.backends.protocols import BackendError, PowerState, Unreachable
from aipanel.config.settings import Settings


class ShutdownParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    silence_minutes: int | None = Field(None, ge=0, le=10080)
    silence_until: datetime | None = None


async def power_is(ctx: ActionContext, state: PowerState) -> bool:
    return await ctx.backends.power.status() == state


async def answers(ctx: ActionContext) -> bool:
    await ctx.host_status()  # raises while the host does not answer
    return True


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


def require_stop_allowed(ctx: ActionContext) -> None:
    require_host_up(ctx)
    require_idle_proxmox(ctx)
    require_outside_protected(ctx)


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
            raise Refused("the BMC does not answer")

    async def run(self, ctx: ActionContext) -> None:
        boot = ctx.settings.timeouts.boot_seconds
        await ctx.do("BMC: power on", ctx.backends.power.on)
        await ctx.wait_for("the host to answer over SSH", lambda: answers(ctx), boot, 10)
        await wait_until_serving(ctx, boot)
        await ctx.end_silences()


@register
class Shutdown(Action):
    name = "shutdown"
    label = "Shut down"
    group = "power"
    confirm = Confirm.TYPED
    power = True
    params = ShutdownParams
    warning = "Stops the LLM's guest, then the host with every guest on it."

    def limit(self, s: Settings) -> float:
        return s.timeouts.command_seconds + s.timeouts.shutdown_seconds + 120

    async def check(self, ctx: ActionContext) -> None:
        require_stop_allowed(ctx)

    async def run(self, ctx: ActionContext) -> None:
        params: ShutdownParams = ctx.params
        now = ctx.clock.now()
        minutes = params.silence_minutes
        if minutes is None:
            minutes = ctx.settings.silences.shutdown_minutes
        until = params.silence_until or now + timedelta(minutes=minutes)
        if until > now:
            await ctx.silence(until, "planned shutdown")
        try:
            await ctx.agent("shutdown")
        except Unreachable as e:  # the host may close the connection as it goes down
            ctx.log(f"the connection closed ({e}); checking the power")
        await ctx.wait_for(
            "the BMC to report power off",
            lambda: power_is(ctx, "off"),
            ctx.settings.timeouts.shutdown_seconds,
            10,
        )


@register
class Reboot(Action):
    name = "reboot"
    label = "Reboot"
    group = "power"
    confirm = Confirm.TYPED
    power = True
    warning = "Restarts the host: every guest on it stops and starts again."

    def limit(self, s: Settings) -> float:
        t = s.timeouts
        return t.command_seconds + t.shutdown_seconds + 2 * t.boot_seconds + t.llm_ready_seconds

    async def check(self, ctx: ActionContext) -> None:
        require_stop_allowed(ctx)

    async def run(self, ctx: ActionContext) -> None:
        t = ctx.settings.timeouts
        before = await boot_id(ctx)
        await ctx.silence(
            ctx.clock.now() + timedelta(minutes=ctx.settings.silences.reboot_minutes), "reboot"
        )
        try:
            await ctx.agent("reboot")
        except Unreachable as e:
            ctx.log(f"the connection closed ({e}); waiting for the host")

        async def gone() -> bool:
            return await boot_id(ctx) != before

        async def back() -> bool:
            now = await boot_id(ctx)
            return now is not None and now != before

        await ctx.wait_for("the host to go down", gone, t.shutdown_seconds, 5)
        await ctx.wait_for("the host to answer again", back, t.boot_seconds, 10)
        await wait_until_serving(ctx, t.boot_seconds)
        await ctx.end_silences()


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
        await ctx.silence(ctx.clock.now() + timedelta(minutes=minutes), f"emergency {self.command}")
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
