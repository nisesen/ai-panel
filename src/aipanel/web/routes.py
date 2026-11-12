"""The pages, and the htmx fragments that keep them current.

Every form works without JavaScript: a POST answers with a redirect (303) and a one-time
message. htmx (`hx-boost`) makes the same forms and links swap the page in place, and polls
the status and the running action.
"""

from datetime import datetime, timedelta
from functools import partial
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.datastructures import FormData

from aipanel import __version__, yamlio
from aipanel.actions.base import Refused
from aipanel.auth import csrf_token
from aipanel.config.store import SettingsError
from aipanel.web.deps import csrf_guard, panel_of, user_of
from aipanel.web.forms import changes_from_form, sections
from aipanel.web.views import action_groups, local, templates, timeline

router = APIRouter(dependencies=[Depends(csrf_guard)], default_response_class=HTMLResponse)

PARAM_FIELDS = ("model", "guest", "op", "silence_minutes")


def render(request: Request, template: str, **context: Any) -> HTMLResponse:
    panel, user = panel_of(request), user_of(request)
    site = panel.site
    banners = []
    if panel.demo:
        banners.append(("info", "DEMO mode: a simulated host. Nothing real is touched."))
    if site.actions.dry_run:
        banners.append(("warn", "DRY-RUN: everything is read for real; changes are only logged."))
    if not panel.loader.valid:
        banners.append(
            (
                "error",
                f"config.yaml is not valid; the last good one is in use. {panel.loader.error}",
            )
        )
    if panel.store.error:
        banners.append(("error", panel.store.error))
    conflict = panel.store.conflict()
    if conflict:
        banners.append(("error", f"The settings disagree with the site config: {conflict}"))
    if not panel.state_writable:
        banners.append(
            (
                "warn",
                "The state directory is not writable: settings, the schedule's "
                "state and the audit log are not saved.",
            )
        )
    base = {
        "panel": panel,
        "site": site,
        "settings": panel.store.get(),
        "user": user,
        "csrf": csrf_token(panel.csrf_key, user),
        "flash": panel.flash.pop(user.name, None),
        "banners": banners,
        "version": __version__,
        "local": partial(local, site),
        "now": panel.clock.now(),
    }
    return templates.TemplateResponse(request, template, base | context)


def back(request: Request, url: str, kind: str, message: str) -> RedirectResponse:
    panel_of(request).flash[user_of(request).name] = (kind, message)
    return RedirectResponse(url, status_code=303)


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""


# Dashboard


def dashboard_context(request: Request) -> dict[str, Any]:
    panel = panel_of(request)
    runner = panel.runner
    upcoming = panel.scheduler.upcoming(days=2)
    snapshot = panel.poller.snapshot
    return {
        "snap": snapshot,
        "job": runner.current or (runner.history[0] if runner.history else None),
        "running": runner.current is not None,
        "groups": action_groups(panel.site),
        "next_event": upcoming[0] if upcoming else None,
        "cost_now": panel.cost.now(snapshot),
        "last_failed": runner.last_failed,
        "schedule": panel.store.get().schedule,
        "next_on": panel.scheduler.next_on(panel.clock.now()),
    }


@router.get("/")
async def dashboard(request: Request) -> HTMLResponse:
    return render(request, "dashboard.html", page="dashboard", **dashboard_context(request))


@router.get("/fragments/status")
async def status_fragment(request: Request) -> HTMLResponse:
    return render(request, "_status.html", **dashboard_context(request))


@router.get("/fragments/job")
async def job_fragment(request: Request) -> HTMLResponse:
    return render(request, "_job.html", **dashboard_context(request))


@router.post("/actions/{name}")
async def start_action(request: Request, name: str) -> RedirectResponse:
    panel, user = panel_of(request), user_of(request)
    form = await request.form()
    params: dict[str, Any] = {k: _text(form, k) for k in PARAM_FIELDS if _text(form, k)}
    if _text(form, "silence") == "next_on":
        on_at = panel.scheduler.next_on(panel.clock.now())
        if on_at is not None:
            grace = panel.store.get().schedule.boot_grace_minutes
            params["silence_until"] = (on_at + timedelta(minutes=grace)).isoformat()
    elif _text(form, "silence"):
        params["silence_minutes"] = _text(form, "silence")
    try:
        job = await panel.runner.start(name, user.name, params, _text(form, "confirm"))
    except Refused as e:
        return back(request, "/", "error", f"Not started: {e}")
    return back(request, "/", "ok", f"Started: {job.label}")


# Schedule


@router.get("/schedule")
async def schedule_page(request: Request) -> HTMLResponse:
    panel = panel_of(request)
    upcoming = panel.scheduler.upcoming(days=7)
    return render(
        request,
        "schedule.html",
        page="schedule",
        upcoming=upcoming,
        days=timeline(panel.site, upcoming, panel.clock.now()),
        state=panel.scheduler.state,
        weekdays=("mon", "tue", "wed", "thu", "fri", "sat", "sun"),
    )


def _change_settings(request: Request, changes: dict[str, Any], url: str, ok: str) -> Response:
    panel, user = panel_of(request), user_of(request)
    try:
        panel.store.update(changes, user.name)
    except SettingsError as e:
        return back(request, url, "error", f"Not saved: {e}")
    return back(request, url, "ok", ok)


@router.post("/schedule/enabled")
async def schedule_enabled(request: Request) -> Response:
    enable = _text(await request.form(), "enabled") == "true"
    message = "The schedule runs." if enable else "The schedule is off."
    return _change_settings(request, {"schedule.enabled": enable}, "/schedule", message)


@router.post("/schedule/rules")
async def add_rule(request: Request) -> Response:
    form = await request.form()
    rule = {
        "name": _text(form, "name"),
        "days": [d for d in form.getlist("days") if isinstance(d, str)],
        "off": _text(form, "off"),
        "on": _text(form, "on"),
    }
    rules = [r.model_dump() for r in panel_of(request).store.get().schedule.rules]
    return _change_settings(
        request, {"schedule.rules": [*rules, rule]}, "/schedule", f"Rule {rule['name']} added."
    )


@router.post("/schedule/rules/{name}/delete")
async def delete_rule(request: Request, name: str) -> Response:
    rules = [r.model_dump() for r in panel_of(request).store.get().schedule.rules]
    kept = [r for r in rules if r["name"] != name]
    return _change_settings(request, {"schedule.rules": kept}, "/schedule", f"Rule {name} removed.")


@router.post("/schedule/rules/{name}/toggle")
async def toggle_rule(request: Request, name: str) -> Response:
    rules = [r.model_dump() for r in panel_of(request).store.get().schedule.rules]
    for r in rules:
        if r["name"] == name:
            r["enabled"] = not r["enabled"]
    return _change_settings(
        request, {"schedule.rules": rules}, "/schedule", f"Rule {name} changed."
    )


@router.post("/schedule/skip")
async def skip(request: Request) -> Response:
    form = await request.form()
    panel, user = panel_of(request), user_of(request)
    boundary = _text(form, "boundary")
    try:
        if _text(form, "undo"):
            panel.scheduler.unskip(boundary, user.name)
        else:
            panel.scheduler.skip(boundary, user.name)
    except ValueError as e:
        return back(request, _text(form, "next") or "/schedule", "error", str(e))
    return back(request, _text(form, "next") or "/schedule", "ok", "Schedule updated.")


@router.post("/schedule/hold")
async def hold(request: Request) -> Response:
    form = await request.form()
    panel, user = panel_of(request), user_of(request)
    kind = _text(form, "kind")
    try:
        until = datetime.fromisoformat(_text(form, "until")).replace(tzinfo=panel.site.site.tz)
        if kind not in ("on", "off"):
            raise ValueError("choose on or off")
        panel.scheduler.hold("on" if kind == "on" else "off", until, user.name)
    except ValueError as e:
        return back(request, "/schedule", "error", f"No hold: {e}")
    return back(request, "/schedule", "ok", f"Held {kind} until {local(panel.site, until)}.")


@router.post("/schedule/holds/clear")
async def clear_holds(request: Request) -> Response:
    panel_of(request).scheduler.clear_holds(user_of(request).name)
    return back(request, "/schedule", "ok", "Holds cleared.")


# Cost, activity, settings


@router.get("/cost")
async def cost_page(request: Request) -> HTMLResponse:
    panel = panel_of(request)
    report = await panel.cost.report(panel.poller.snapshot)
    return render(request, "cost.html", page="cost", report=report)


@router.get("/activity")
async def activity_page(request: Request, q: str = "", actor: str = "") -> HTMLResponse:
    panel = panel_of(request)
    rows = panel.audit.read(panel.store.get().display.activity_rows, q, actor)
    return render(request, "activity.html", page="activity", rows=rows, q=q, actor=actor)


@router.get("/settings")
async def settings_page(request: Request) -> HTMLResponse:
    panel = panel_of(request)
    return render(request, "settings.html", page="settings", sections=sections(panel.store.get()))


@router.post("/settings")
async def save_settings(request: Request) -> Response:
    form = await request.form()
    changes = changes_from_form(form, panel_of(request).store.get())
    return _change_settings(request, changes, "/settings", "Settings saved.")


@router.get("/settings/export")
async def export_settings(request: Request) -> PlainTextResponse:
    text = panel_of(request).store.export_yaml()
    headers = {"Content-Disposition": 'attachment; filename="ai-panel-settings.yaml"'}
    return PlainTextResponse(text, media_type="application/yaml", headers=headers)


@router.post("/settings/import")
async def import_settings(request: Request) -> Response:
    form = await request.form()
    panel, user = panel_of(request), user_of(request)
    try:
        data = yamlio.load(_text(form, "yaml"))
        if not isinstance(data, dict):
            raise SettingsError("the YAML must be a mapping")
        panel.store.replace(data, user.name)
    except (SettingsError, ValueError) as e:
        return back(request, "/settings", "error", f"Not imported: {e}")
    return back(request, "/settings", "ok", "Settings imported.")
