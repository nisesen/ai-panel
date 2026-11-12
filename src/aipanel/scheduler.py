"""The scheduler: power the host off and on at the boundaries of the schedule's rules.

Edge-triggered, not desired-state: it acts only when a boundary (an `off` or `on` time) passes,
so someone who powers the host on inside an off window keeps it on until the next boundary.

- Catch-up: a boundary missed while the panel was down is acted on within
  `catch_up_minutes`, otherwise skipped and logged. Each boundary is handled once (the handled
  ones are kept in the state directory).
- Busy at an off boundary (LLM slots, a Proxmox task, another action): try again every
  `busy_retry_minutes` for up to `busy_grace_minutes`, then skip, or shut down anyway with
  `force_after_grace`.
- Protected windows: a stop that would land inside one is refused.
- Holds and skips are runtime state: skip one boundary; hold on (or off) until a time.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from aipanel.actions.base import Refused, protected_window
from aipanel.audit import Audit
from aipanel.clock import Clock
from aipanel.config.settings import ScheduleRule, ScheduleSettings, Settings
from aipanel.config.site import SiteConfig
from aipanel.files import atomic_write
from aipanel.metrics import PanelMetrics
from aipanel.poller import Poller, Snapshot
from aipanel.runner import Runner
from aipanel.weekly import WEEKDAYS, minutes

log = logging.getLogger("aipanel.scheduler")

ACTOR = "scheduler"
TICK_SECONDS = 30
KEEP_DAYS = 14

Kind = Literal["off", "on"]


@dataclass(frozen=True)
class Boundary:
    rule: str
    kind: Kind
    at: datetime  # UTC

    @property
    def id(self) -> str:
        return f"{self.rule}|{self.kind}|{self.at.isoformat()}"


def _at(day: date, hhmm: str, tz: ZoneInfo) -> datetime:
    hours, mins = hhmm.split(":")
    return datetime.combine(day, time(int(hours), int(mins)), tzinfo=tz).astimezone(UTC)


def fingerprint(rule: ScheduleRule) -> str:
    return f"{rule.name}|{','.join(rule.days)}|{rule.off}|{rule.on}"


def boundaries(
    rules: Sequence[ScheduleRule], tz: ZoneInfo, start: datetime, end: datetime
) -> list[Boundary]:
    """Every boundary of the enabled rules in [start, end), in time order."""
    out = []
    day = start.astimezone(tz).date() - timedelta(days=1)
    last = end.astimezone(tz).date()
    while day <= last:
        weekday = WEEKDAYS[day.weekday()]
        for rule in rules:
            if not rule.enabled or weekday not in rule.days:
                continue
            on_day = day if minutes(rule.on) > minutes(rule.off) else day + timedelta(days=1)
            off_at, on_at = _at(day, rule.off, tz), _at(on_day, rule.on, tz)
            out += [Boundary(rule.name, "off", off_at), Boundary(rule.name, "on", on_at)]
        day += timedelta(days=1)
    return sorted((b for b in out if start <= b.at < end), key=lambda b: (b.at, b.kind))


class Pending(BaseModel):
    """An off boundary waiting for the host to be idle."""

    id: str
    at: datetime
    first: datetime
    next: datetime
    reason: str


class Handled(BaseModel):
    id: str
    rule: str
    kind: Kind
    at: datetime
    result: str
    ok: bool
    handled_at: datetime


class State(BaseModel):
    last_tick: datetime | None = None
    done: dict[str, str] = Field(default_factory=dict)  # boundary id -> what happened
    skip: list[str] = Field(default_factory=list)
    hold_on_until: datetime | None = None
    hold_off_until: datetime | None = None
    pending: Pending | None = None
    last: Handled | None = None
    seen: dict[str, datetime] = Field(default_factory=dict)  # rule fingerprint -> first seen
    enabled_since: datetime | None = None


class Scheduler:
    def __init__(
        self,
        path: Path | None,
        site: Callable[[], SiteConfig],
        settings: Callable[[], Settings],
        runner: Runner,
        poller: Poller,
        clock: Clock,
        audit: Audit,
        metrics: PanelMetrics,
    ) -> None:
        self.path = path
        self.site = site
        self.settings = settings
        self.runner = runner
        self.poller = poller
        self.clock = clock
        self.audit = audit
        self.metrics = metrics
        self.state = State()
        if path is not None and path.exists():
            try:
                self.state = State.model_validate_json(path.read_text())
            except ValueError as e:
                log.error("scheduler state %s unreadable, starting afresh: %s", path, e)

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            atomic_write(self.path, self.state.model_dump_json(indent=2) + "\n")
        except OSError as e:
            log.error("scheduler state not saved: %s", e)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            await self.clock.sleep(TICK_SECONDS)

    # The tick

    async def tick(self) -> None:
        schedule, site, now = self.settings().schedule, self.site(), self.clock.now()
        st = self.state
        self.metrics.set("aipanel_schedule_enabled", 1 if schedule.enabled else 0)
        self._tidy(now)
        if not schedule.enabled:
            st.enabled_since, st.pending, st.last_tick = None, None, now
            self._save()
            return
        if st.enabled_since is None:
            st.enabled_since = now
        for rule in schedule.rules:
            st.seen.setdefault(fingerprint(rule), now)

        catch_up = timedelta(minutes=schedule.catch_up_minutes)
        since = max(min(st.last_tick or now, now - catch_up), now - timedelta(days=7))
        if st.pending is not None:
            since = min(since, st.pending.at)
        first_seen = {r.name: st.seen[fingerprint(r)] for r in schedule.rules}
        due = [
            b
            for b in boundaries(schedule.rules, site.site.tz, since, now + timedelta(seconds=1))
            if b.id not in st.done and b.at >= max(st.enabled_since, first_seen[b.rule])
        ]
        if due:
            *older, latest = due
            for b in older:
                late = now - b.at > catch_up and not (st.pending and st.pending.id == b.id)
                self._mark(
                    b,
                    now,
                    "missed: the panel was not running"
                    if late
                    else "superseded by a later boundary",
                    ok=not late,
                )
            if st.pending is not None and st.pending.id != latest.id:
                st.pending = None
            waiting = st.pending is not None
            if not waiting and now - latest.at > catch_up:
                self._mark(
                    latest, now, f"missed: more than {schedule.catch_up_minutes} min late", ok=False
                )
            else:
                await self._act(latest, now, schedule, site)
        st.last_tick = now
        self._save()

    async def _act(
        self, b: Boundary, now: datetime, schedule: ScheduleSettings, site: SiteConfig
    ) -> None:
        st = self.state
        if b.id in st.skip:
            st.skip.remove(b.id)
            self._mark(b, now, "skipped, as asked", ok=True)
            return
        if b.kind == "off" and st.hold_on_until and now < st.hold_on_until:
            self._mark(b, now, f"held on until {self._local(st.hold_on_until)}", ok=True)
            return
        if b.kind == "on" and st.hold_off_until and now < st.hold_off_until:
            self._mark(b, now, f"held off until {self._local(st.hold_off_until)}", ok=True)
            return
        if st.pending is not None and now < st.pending.next:
            return  # waiting to try again
        snapshot = await self.poller.refresh()
        if b.kind == "on":
            await self._power_on(b, now, snapshot, site)
        else:
            await self._power_off(b, now, snapshot, schedule, site)

    async def _power_on(self, b: Boundary, now: datetime, snap: Snapshot, site: SiteConfig) -> None:
        if snap.power == "on":
            self._mark(b, now, "on already", ok=True)
            return
        try:
            job = await self.runner.start("power_on", ACTOR, confirmation=site.site.name)
        except Refused as e:
            if not e.retryable:  # a retryable refusal is tried again on the next tick
                self._mark(b, now, f"refused: {e}", ok=False)
            return
        self._mark(b, now, f"started power_on (job {job.id})", ok=True)

    async def _power_off(
        self,
        b: Boundary,
        now: datetime,
        snap: Snapshot,
        schedule: ScheduleSettings,
        site: SiteConfig,
    ) -> None:
        st = self.state
        if snap.power == "off":
            st.pending = None
            self._mark(b, now, "off already", ok=True)
            return
        window = protected_window(site, now)
        if window is not None:
            st.pending = None
            self._mark(b, now, f"refused: inside the protected window {window.reason!r}", ok=False)
            return
        busy = snap.busy_reason() or ("another action is running" if self.runner.busy else None)
        if busy is None:
            try:
                await self._shutdown(b, now, schedule, site, force=False)
                return
            except Refused as e:
                if not e.retryable:
                    st.pending = None
                    self._mark(b, now, f"refused: {e}", ok=False)
                    return
                busy = str(e)
        pending = st.pending or Pending(id=b.id, at=b.at, first=now, next=now, reason=busy)
        grace = timedelta(minutes=schedule.busy_grace_minutes)
        if now - pending.first < grace:
            pending.next = now + timedelta(minutes=schedule.busy_retry_minutes)
            pending.reason = busy
            st.pending = pending
            self.audit.record(
                "schedule.busy",
                ACTOR,
                boundary=b.id,
                reason=busy,
                retry_at=pending.next.isoformat(),
            )
            return
        st.pending = None
        if not schedule.force_after_grace:
            self._mark(
                b, now, f"skipped: busy for {schedule.busy_grace_minutes} min ({busy})", ok=False
            )
            return
        try:
            await self._shutdown(b, now, schedule, site, force=True, why=busy)
        except Refused as e:
            self._mark(b, now, f"refused: {e}", ok=False)

    async def _shutdown(
        self,
        b: Boundary,
        now: datetime,
        schedule: ScheduleSettings,
        site: SiteConfig,
        force: bool,
        why: str = "",
    ) -> None:
        on_at = self.next_on(now)
        params = {}
        if on_at is not None:
            until = on_at + timedelta(minutes=schedule.boot_grace_minutes)
            params["silence_until"] = until.isoformat()
        job = await self.runner.start(
            "shutdown", ACTOR, params, confirmation=site.site.name, force=force
        )
        self.state.pending = None
        forced = f", forced although busy ({why})" if force else ""
        self._mark(b, now, f"started shutdown (job {job.id}){forced}", ok=True)

    def _mark(self, b: Boundary, now: datetime, result: str, ok: bool) -> None:
        self.state.done[b.id] = result
        self.state.last = Handled(
            id=b.id, rule=b.rule, kind=b.kind, at=b.at, result=result, ok=ok, handled_at=now
        )
        self.audit.record(
            "schedule.boundary",
            ACTOR,
            rule=b.rule,
            kind=b.kind,
            at=b.at.isoformat(),
            result=result,
            ok=ok,
        )
        self.metrics.set("aipanel_schedule_last_boundary_timestamp", now.timestamp())
        self.metrics.set("aipanel_schedule_last_boundary_success", 1 if ok else 0)

    def _tidy(self, now: datetime) -> None:
        st = self.state
        if st.hold_on_until is not None and st.hold_on_until <= now:
            st.hold_on_until = None
        if st.hold_off_until is not None and st.hold_off_until <= now:
            st.hold_off_until = None
        oldest = now - timedelta(days=KEEP_DAYS)
        st.done = {k: v for k, v in st.done.items() if _when(k) >= oldest}
        st.skip = [k for k in st.skip if _when(k) >= now - timedelta(days=1)]

    def _local(self, when: datetime) -> str:
        return when.astimezone(self.site().site.tz).strftime("%a %d %b %H:%M")

    # What the page and the API show and change

    def upcoming(self, days: int = 7) -> list[tuple[Boundary, str]]:
        """The next boundaries, each with what will happen to it ("" when it will run)."""
        now, st = self.clock.now(), self.state
        rules = self.settings().schedule.rules
        out = []
        for b in boundaries(rules, self.site().site.tz, now, now + timedelta(days=days)):
            note = ""
            if b.id in st.skip:
                note = "skipped"
            elif b.kind == "off" and st.hold_on_until and b.at < st.hold_on_until:
                note = "held on"
            elif b.kind == "on" and st.hold_off_until and b.at < st.hold_off_until:
                note = "held off"
            out.append((b, note))
        return out

    def next_on(self, after: datetime) -> datetime | None:
        rules = self.settings().schedule.rules
        st = self.state
        for b in boundaries(rules, self.site().site.tz, after, after + timedelta(days=8)):
            held = st.hold_off_until is not None and b.at < st.hold_off_until
            if b.kind == "on" and b.id not in st.skip and not held:
                return b.at
        return None

    def skip(self, boundary_id: str, actor: str) -> None:
        if boundary_id not in {b.id for b, _ in self.upcoming(days=8)}:
            raise ValueError("no such upcoming boundary")
        if boundary_id not in self.state.skip:
            self.state.skip.append(boundary_id)
        self.audit.record("schedule.skip", actor, boundary=boundary_id)
        self._save()

    def unskip(self, boundary_id: str, actor: str) -> None:
        if boundary_id in self.state.skip:
            self.state.skip.remove(boundary_id)
            self.audit.record("schedule.unskip", actor, boundary=boundary_id)
            self._save()

    def hold(self, kind: Kind, until: datetime, actor: str) -> None:
        now = self.clock.now()
        if until <= now:
            raise ValueError("a hold must end in the future")
        if until > now + timedelta(days=31):
            raise ValueError("a hold may last 31 days at most")
        if kind == "on":
            self.state.hold_on_until, self.state.hold_off_until = until, None
        else:
            self.state.hold_off_until, self.state.hold_on_until = until, None
        self.audit.record("schedule.hold", actor, kind=kind, until=until.isoformat())
        self._save()

    def clear_holds(self, actor: str) -> None:
        self.state.hold_on_until = self.state.hold_off_until = None
        self.audit.record("schedule.hold_cleared", actor)
        self._save()


def _when(boundary_id: str) -> datetime:
    try:
        return datetime.fromisoformat(boundary_id.rsplit("|", 1)[1])
    except IndexError, ValueError:
        return datetime.min.replace(tzinfo=UTC)
