"""The LLM: switch the model, restart, stop or start it, through llm-ctl.

Each reads the result back from llama.cpp itself (/health, and /props for which model is
loaded): what was asked for is not proof of what is serving. A client's own checks can be
paused around them (`llm.pause_hermes_checks`), so a planned switch does not page anyone.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from aipanel.actions.base import (
    Action,
    ActionContext,
    ActionFailed,
    Confirm,
    Refused,
    require_host_up,
)
from aipanel.actions.registry import register
from aipanel.backends.protocols import BackendError
from aipanel.config.settings import Settings
from aipanel.config.site import SiteConfig, Token


class ModelParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model: Token


async def read_back(ctx: ActionContext, expect: str | None) -> None:
    llm = ctx.backends.llm
    await ctx.wait_for(
        "the LLM to report healthy", llm.health, ctx.settings.timeouts.llm_ready_seconds, 5
    )
    if ctx.dry_run:
        return
    props = await llm.props()
    alias, path = props.get("model_alias", "?"), props.get("model_path", "?")
    ctx.log(f"serving: {alias} ({path})")
    wanted = ctx.site.model(expect) if expect else None
    if wanted is not None and wanted.match and wanted.match not in f"{alias} {path}":
        raise ActionFailed(f"/props reports {alias}, not {wanted.label}")


class LlmAction(Action):
    group = "llm"

    def limit(self, s: Settings) -> float:
        return 3 * s.timeouts.command_seconds + s.timeouts.llm_ready_seconds + 60

    async def check(self, ctx: ActionContext) -> None:
        require_host_up(ctx)

    async def run(self, ctx: ActionContext) -> None:
        pause = ctx.site.llm.pause_hermes_checks
        if pause:
            await ctx.agent("hermes-checks", "pause")
        try:
            await self.steps(ctx)
        finally:
            if pause:
                await ctx.agent("hermes-checks", "resume")

    async def steps(self, ctx: ActionContext) -> None:
        raise NotImplementedError


@register
class LlmSwitch(LlmAction):
    name = "llm_switch"
    label = "Switch model"
    params = ModelParams

    def parse(self, raw: Mapping[str, Any], site: SiteConfig) -> BaseModel:
        params = super().parse(raw, site)
        assert isinstance(params, ModelParams)
        if site.model(params.model) is None:
            raise Refused(f"unknown model {params.model!r}")
        return params

    async def check(self, ctx: ActionContext) -> None:
        require_host_up(ctx)
        if ctx.snapshot.llm.healthy and ctx.snapshot.llm.model == ctx.params.model:
            raise Refused(f"{ctx.params.model} is serving already")

    async def steps(self, ctx: ActionContext) -> None:
        await ctx.agent("llm", "switch", ctx.params.model)
        await read_back(ctx, ctx.params.model)


@register
class LlmRestart(LlmAction):
    name = "llm_restart"
    label = "Restart LLM"

    async def steps(self, ctx: ActionContext) -> None:
        await ctx.agent("llm", "restart")
        await read_back(ctx, ctx.snapshot.llm.model)


@register
class LlmStart(LlmAction):
    name = "llm_start"
    label = "Start LLM"

    async def check(self, ctx: ActionContext) -> None:
        require_host_up(ctx)
        if ctx.snapshot.llm.healthy:
            raise Refused("the LLM is serving already")

    async def steps(self, ctx: ActionContext) -> None:
        await ctx.agent("llm", "start")
        await read_back(ctx, None)


@register
class LlmStop(LlmAction):
    name = "llm_stop"
    label = "Stop LLM"
    confirm = Confirm.CONFIRM
    warning = "Every client of the LLM gets errors until it is started again."

    async def steps(self, ctx: ActionContext) -> None:
        llm = ctx.backends.llm

        async def stopped() -> bool:
            try:
                return not await llm.health()
            except BackendError:
                return True

        await ctx.agent("llm", "stop")
        await ctx.wait_for("the LLM to stop answering", stopped, 120, 5)
