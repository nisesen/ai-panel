"""What an action is, and the context it runs in.

An action has a name, a label, a confirmation level, parameters (a pydantic model whose values
are checked against the site config's lists, never free text), preconditions (`check`), and
its steps (`run`). Steps that change something go through `ctx.do`, and waits through
`ctx.wait_for`: in dry-run mode the first only logs, and the second does not wait.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from aipanel import weekly
from aipanel.backends import Backends
from aipanel.backends.protocols import BackendError
from aipanel.clock import Clock
from aipanel.config.settings import Settings
from aipanel.config.site import ProtectedWindow, SiteConfig
from aipanel.poller import STATUS_TIMEOUT, Snapshot

BMC_FRESH = timedelta(minutes=2)


class Confirm(StrEnum):
    NONE = "none"  # read-only
    CLICK = "click"  # a button
    CONFIRM = "confirm"  # a button and "are you sure"
    TYPED = "typed"  # type the host's name


class Refused(Exception):
    """The action did not start. `retryable`: it may start later (busy, cooling down)."""

    def __init__(self, reason: str, retryable: bool = False) -> None:
        super().__init__(reason)
        self.retryable = retryable


class ActionFailed(Exception):
    pass


class ActionTimeout(ActionFailed):
    pass


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass
class ActionContext:
    params: Any  # the action's own params model
    actor: str
    site: SiteConfig
    settings: Settings
    snapshot: Snapshot
    backends: Backends
    clock: Clock
    log: Callable[[str], None]
    dry_run: bool = False
    force: bool = False  # the scheduler, after its grace time: busy does not stop it
    last_failed: str | None = None  # the last power action, if it failed (emergency actions)
    next_on: datetime | None = None  # the schedule's next power on, if it runs
    silenced: list[str] = field(default_factory=list)  # the silences this action made

    async def do[T](self, what: str, step: Callable[[], Awaitable[T]]) -> T | None:
        """A step that changes something."""
        if self.dry_run:
            self.log(f"DRY-RUN, not done: {what}")
            return None
        self.log(what)
        return await step()

    async def wait_for(
        self,
        what: str,
        check: Callable[[], Awaitable[bool]],
        timeout: float,
        interval: float = 5.0,
    ) -> None:
        if self.dry_run:
            self.log(f"DRY-RUN, not waited for: {what}")
            return
        self.log(f"waiting for {what} (up to {timeout:g} s)")
        deadline = self.clock.now() + timedelta(seconds=timeout)
        while True:
            try:
                if await check():
                    self.log(f"done: {what}")
                    return
            except BackendError:
                pass  # not there yet
            if self.clock.now() >= deadline:
                raise ActionTimeout(f"timed out after {timeout:g} s waiting for {what}")
            await self.clock.sleep(interval)

    async def agent(self, *args: str) -> dict[str, Any]:
        """Run an llm-ctl verb that changes something (logged only in dry-run)."""
        timeout = self.settings.timeouts.command_seconds
        reply = await self.do(
            f"llm-ctl {' '.join(args)}", lambda: self.backends.agent.call(args, timeout)
        )
        return reply or {}

    async def host_status(self) -> dict[str, Any]:
        return await self.backends.agent.call(["status"], STATUS_TIMEOUT)

    async def silence(self, until: datetime, why: str) -> None:
        """One silence per matcher set of the site config, all ending at `until`."""
        if self.backends.silences is None or self.site.alertmanager is None:
            self.log("no Alertmanager configured: nothing silenced")
            return
        silences = self.backends.silences
        comment = f"ai-panel: {why} (by {self.actor})"
        local = until.astimezone(self.site.site.tz).strftime("%a %d %b %Y %H:%M %Z")
        sets = len(self.site.alertmanager.silences)
        ids = await self.do(
            f"silence alerts until {local} ({sets} silence{'s' if sets > 1 else ''})",
            lambda: silences.create(until, comment),
        )
        self.silenced += ids or []

    async def unsilence(self, ids: list[str] | None = None) -> None:
        """Expire the given silences, or every one the panel made (its createdBy), and only
        those. A failure here is a warning: it never fails an action that did its work."""
        silences = self.backends.silences
        if silences is None:
            return
        try:
            for silence_id in ids if ids is not None else [s.id for s in await silences.active()]:
                await self.do(f"end silence {silence_id}", partial(silences.expire, silence_id))
        except Exception as e:
            self.log(f"warning: silences not ended ({e}); they end at their set time")


class Action:
    name: ClassVar[str]
    label: ClassVar[str]
    group: ClassVar[str]
    confirm: ClassVar[Confirm] = Confirm.CLICK
    power: ClassVar[bool] = False  # counts for the cooldown between power actions
    params: ClassVar[type[BaseModel]] = NoParams
    warning: ClassVar[str] = ""

    def parse(self, raw: Mapping[str, Any], site: SiteConfig) -> BaseModel:
        try:
            return self.params.model_validate(dict(raw))
        except ValidationError as e:
            raise Refused(f"bad parameters: {e.errors()[0]['msg']}") from None

    def limit(self, settings: Settings) -> float:
        """How long the whole action may take before it is given up."""
        return float(settings.timeouts.command_seconds) + 60

    async def check(self, ctx: ActionContext) -> None:
        """Raise Refused when the action may not start now."""

    async def run(self, ctx: ActionContext) -> None:
        raise NotImplementedError

    async def recover(self, ctx: ActionContext) -> None:
        """After `run` failed or timed out: undo what should not outlast the failure."""


# Preconditions several actions share


def require_host_up(ctx: ActionContext) -> None:
    if ctx.snapshot.power == "off":
        raise Refused("the host is off")
    if ctx.snapshot.host is None:
        raise Refused("the host does not answer over SSH", retryable=True)


def require_bmc_answering(ctx: ActionContext) -> None:
    """The BMC is the only way to switch the host on again: no stop without it."""
    at = ctx.snapshot.power_at
    if ctx.snapshot.power != "on" or at is None or ctx.clock.now() - at > BMC_FRESH:
        raise Refused(
            "the BMC has not answered in the last 2 minutes: without it the host could not be "
            "switched on again",
            retryable=True,
        )


def require_idle_proxmox(ctx: ActionContext) -> None:
    host = ctx.snapshot.host
    if host is not None and host.tasks and not ctx.force:
        raise Refused(f"Proxmox is running a task: {', '.join(host.tasks)}", retryable=True)


def protected_window(site: SiteConfig, when: datetime) -> ProtectedWindow | None:
    local = when.astimezone(site.site.tz)
    for window in site.protected_windows:
        if weekly.contains(weekly.spans(window.days, window.start, window.end), local):
            return window
    return None


def require_outside_protected(ctx: ActionContext) -> None:
    window = protected_window(ctx.site, ctx.clock.now())
    if window is not None:
        raise Refused(
            f"inside the protected window {window.reason!r} ({window.start}-{window.end})"
        )
