"""The runner: one action at a time, a cooldown between power actions, a step log the page
shows as it grows, and an audit record of who started what and how it ended.

How each action and power action last ended is kept in the state directory (`runner.json`), so
the failure metrics and the emergency actions' gate survive a restart.
"""

import asyncio
import contextlib
import json
import logging
import secrets
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from aipanel import files
from aipanel.actions import registry
from aipanel.actions.base import (
    Action,
    ActionContext,
    ActionFailed,
    ActionTimeout,
    Confirm,
    Refused,
)
from aipanel.audit import Audit
from aipanel.backends import Backends
from aipanel.backends.protocols import BackendError
from aipanel.clock import Clock
from aipanel.config.settings import Settings
from aipanel.config.site import SiteConfig
from aipanel.metrics import PanelMetrics
from aipanel.poller import Poller

log = logging.getLogger("aipanel.runner")

Status = Literal["running", "succeeded", "failed", "timed_out"]


@dataclass(frozen=True)
class Step:
    at: datetime
    message: str


@dataclass
class Job:
    id: str
    action: str
    label: str
    actor: str
    params: dict[str, Any]
    started_at: datetime
    dry_run: bool
    status: Status = "running"
    finished_at: datetime | None = None
    error: str | None = None
    steps: list[Step] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.status == "running"


def confirmed(action: Action, confirmation: str | None, site: SiteConfig) -> bool:
    given = (confirmation or "").strip()
    match action.confirm:
        case Confirm.TYPED:
            return given == site.site.name
        case Confirm.CONFIRM:
            return given.lower() in ("yes", "true", "on") or given == site.site.name
        case _:
            return True


class Runner:
    def __init__(
        self,
        site: Callable[[], SiteConfig],
        settings: Callable[[], Settings],
        backends: Callable[[], Backends],
        poller: Poller,
        clock: Clock,
        audit: Audit,
        metrics: PanelMetrics,
        state: Path | None = None,
        next_on: Callable[[datetime], datetime | None] = lambda now: None,
    ) -> None:
        self.site = site
        self.settings = settings
        self.backends = backends
        self.poller = poller
        self.clock = clock
        self.audit = audit
        self.metrics = metrics
        self.state = state
        self.next_on = next_on
        self.current: Job | None = None
        self.history: deque[Job] = deque(maxlen=20)
        self.last_failed: str | None = None  # the last power action, while it failed
        self.results: dict[str, dict[str, Any]] = {}  # "action|actor" -> {ok, at}
        self._starting = False
        self._last_power: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self._restore()

    def _restore(self) -> None:
        if self.state is None or not self.state.exists():
            return
        try:
            saved = json.loads(self.state.read_text())
            self.last_failed = saved.get("last_failed")
            self.results = dict(saved.get("results") or {})
        except (OSError, ValueError, AttributeError) as e:
            log.error("runner state %s unreadable: %s", self.state, e)
        for key, result in self.results.items():
            action, _, actor = key.partition("|")
            labels = {"action": action, "actor": actor}
            self.metrics.set("aipanel_action_last_success", 1 if result.get("ok") else 0, labels)
            self.metrics.set("aipanel_action_last_timestamp", float(result.get("at", 0)), labels)

    @property
    def busy(self) -> bool:
        return self._starting or self.current is not None

    def job(self, job_id: str) -> Job | None:
        if self.current is not None and self.current.id == job_id:
            return self.current
        return next((j for j in self.history if j.id == job_id), None)

    async def start(
        self,
        name: str,
        actor: str,
        params: Mapping[str, Any] | None = None,
        confirmation: str | None = None,
        force: bool = False,
    ) -> Job:
        """Check and start an action; it runs in the background. Raises Refused."""
        try:
            return await self._start(name, actor, dict(params or {}), confirmation, force)
        except Refused as e:
            self.audit.record("action.refused", actor, action=name, reason=str(e))
            raise

    async def _start(
        self, name: str, actor: str, raw: dict[str, Any], confirmation: str | None, force: bool
    ) -> Job:
        site, settings = self.site(), self.settings()
        action = registry.get(name)
        if action is None or not site.action_enabled(name):
            raise Refused("no such action on this panel")
        params = action.parse(raw, site)
        if not confirmed(action, confirmation, site):
            what = f"type {site.site.name!r}" if action.confirm == Confirm.TYPED else "confirm"
            raise Refused(f"{action.label} needs confirmation: {what}")
        if self.busy:
            running = self.current.label if self.current else "another action"
            raise Refused(f"{running} is running; one action at a time", retryable=True)
        cooldown = timedelta(seconds=settings.cooldowns.power_seconds)
        now = self.clock.now()
        if action.power and self._last_power is not None and now - self._last_power < cooldown:
            wait = (self._last_power + cooldown - now).total_seconds()
            raise Refused(
                f"power actions need {cooldown.seconds} s between them: wait {wait:.0f} s",
                retryable=True,
            )
        self._starting = True
        try:
            snapshot = await self.poller.refresh()
            job = Job(
                id=secrets.token_hex(4),
                action=name,
                label=action.label,
                actor=actor,
                params=_plain(params),
                started_at=now,
                dry_run=site.actions.dry_run,
            )
            ctx = ActionContext(
                params=params,
                actor=actor,
                site=site,
                settings=settings,
                snapshot=snapshot,
                backends=self.backends(),
                clock=self.clock,
                log=lambda message: self._log(job, message),
                dry_run=site.actions.dry_run,
                force=force,
                last_failed=self.last_failed,
                next_on=self.next_on(now),
            )
            await action.check(ctx)
            if action.power:
                self._last_power = now
            self.current = job
        finally:
            self._starting = False
        self.audit.record(
            "action.started", actor, action=name, job=job.id, params=job.params, dry_run=job.dry_run
        )
        self.metrics.set("aipanel_action_running", 1)
        self._task = asyncio.create_task(self._execute(job, action, ctx, action.limit(settings)))
        return job

    def _log(self, job: Job, message: str) -> None:
        job.steps.append(Step(self.clock.now(), message))
        log.info("[%s %s] %s", job.action, job.id, message)

    async def _execute(self, job: Job, action: Action, ctx: ActionContext, limit: float) -> None:
        try:
            await asyncio.wait_for(action.run(ctx), limit)
            job.status = "succeeded"
        except ActionTimeout as e:
            job.status, job.error = "timed_out", str(e)
        except TimeoutError:
            job.status, job.error = "timed_out", f"gave up after {limit:g} s"
        except (ActionFailed, BackendError, Refused) as e:
            job.status, job.error = "failed", str(e)
        except asyncio.CancelledError:
            job.status, job.error = "failed", "interrupted: the panel stopped"
            self._log(job, job.error)
            self._finish(job)
            raise
        except Exception as e:
            log.exception("action %s failed", job.action)
            job.status, job.error = "failed", f"{type(e).__name__}: {e}"
        if job.error:
            self._log(job, job.error)
        if job.status in ("failed", "timed_out"):
            try:
                await asyncio.wait_for(action.recover(ctx), 120)
            except Exception as e:
                ctx.log(f"warning: clean-up after the failure did not finish ({e})")
        self._finish(job)
        with contextlib.suppress(Exception):
            await self.poller.refresh()  # the dashboard shows the new state at once

    def _finish(self, job: Job) -> None:
        job.finished_at = self.clock.now()
        self.current = None
        self.history.appendleft(job)
        ok = job.status == "succeeded"
        if registry.REGISTRY[job.action].power:  # only a later power action clears a failure
            self.last_failed = None if ok else job.action
        self.results[f"{job.action}|{job.actor}"] = {"ok": ok, "at": job.finished_at.timestamp()}
        if self.state is not None:
            text = json.dumps({"last_failed": self.last_failed, "results": self.results})
            state = self.state
            files.in_background(lambda: files.atomic_write(state, text + "\n"))
        self.audit.record(
            "action.finished",
            job.actor,
            action=job.action,
            job=job.id,
            status=job.status,
            error=job.error,
            dry_run=job.dry_run,
            steps=[s.message for s in job.steps],
        )
        labels = {"action": job.action, "actor": job.actor}
        self.metrics.set("aipanel_action_running", 0)
        self.metrics.set("aipanel_action_last_success", 1 if ok else 0, labels)
        self.metrics.set("aipanel_action_last_timestamp", job.finished_at.timestamp(), labels)

    async def stop(self) -> None:
        """Interrupt the running action (the panel is stopping); it is logged as failed."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        await self.wait()

    async def wait(self) -> Job | None:
        """Wait for the running action to end (for the CLI and the tests)."""
        task = self._task
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return self.history[0] if self.history else None


def _plain(params: BaseModel) -> dict[str, Any]:
    return params.model_dump(mode="json", exclude_none=True)
