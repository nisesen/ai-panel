"""Maintenance: the host agent's read-only probe, and pausing or resuming a client's checks."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from aipanel.actions.base import Action, ActionContext, Confirm, require_host_up
from aipanel.actions.registry import register


@register
class Probe(Action):
    name = "probe"
    label = "Probe"
    group = "maintenance"
    confirm = Confirm.NONE

    async def check(self, ctx: ActionContext) -> None:
        require_host_up(ctx)

    async def run(self, ctx: ActionContext) -> None:
        # Read-only, so it runs in dry-run mode too.
        ctx.log("llm-ctl probe")
        reply = await ctx.backends.agent.call(["probe"], ctx.settings.timeouts.command_seconds)
        for key, value in reply.items():
            if key not in ("ok", "verb"):
                ctx.log(f"{key}: {json.dumps(value) if not isinstance(value, str) else value}")


class ChecksParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    op: Literal["pause", "resume"]


@register
class HermesChecks(Action):
    name = "hermes_checks"
    label = "Client checks"
    group = "maintenance"
    params = ChecksParams

    async def check(self, ctx: ActionContext) -> None:
        require_host_up(ctx)

    async def run(self, ctx: ActionContext) -> None:
        await ctx.agent("hermes-checks", ctx.params.op)
