"""Guests (Proxmox containers and VMs): start, shut down, reboot; only the ids the site
config lists."""

from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from aipanel.actions.base import Action, ActionContext, Confirm, Refused, require_host_up
from aipanel.actions.registry import register
from aipanel.config.settings import Settings
from aipanel.config.site import SiteConfig


class GuestParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    guest: int


class GuestAction(Action):
    group = "guests"
    confirm = Confirm.CONFIRM
    params = GuestParams
    op: ClassVar[Literal["start", "shutdown", "reboot"]]
    expect: ClassVar[str]

    def parse(self, raw: Mapping[str, Any], site: SiteConfig) -> BaseModel:
        params = super().parse(raw, site)
        assert isinstance(params, GuestParams)
        if site.guest(params.guest) is None:
            raise Refused(f"guest {params.guest} is not in the site config")
        return params

    def limit(self, s: Settings) -> float:
        return s.timeouts.command_seconds + s.timeouts.guest_seconds + 60

    async def check(self, ctx: ActionContext) -> None:
        require_host_up(ctx)

    async def run(self, ctx: ActionContext) -> None:
        guest_id: int = ctx.params.guest

        async def reached() -> bool:
            guests = (await ctx.host_status()).get("guests") or []
            return any(g.get("id") == guest_id and g.get("status") == self.expect for g in guests)

        await ctx.agent("guest", str(guest_id), self.op)
        await ctx.wait_for(
            f"guest {guest_id} to be {self.expect}", reached, ctx.settings.timeouts.guest_seconds
        )


@register
class GuestStart(GuestAction):
    name = "guest_start"
    label = "Start"
    op = "start"
    expect = "running"


@register
class GuestShutdown(GuestAction):
    name = "guest_shutdown"
    label = "Shut down"
    op = "shutdown"
    expect = "stopped"


@register
class GuestReboot(GuestAction):
    name = "guest_reboot"
    label = "Reboot"
    op = "reboot"
    expect = "running"
