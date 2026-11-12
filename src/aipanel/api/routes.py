"""The JSON API: what the pages show, and the same changes, for scripts and agents.

Changes need the CSRF token in the X-CSRF-Token header; GET /api/session gives it.
"""

import asyncio
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from aipanel.actions import registry
from aipanel.actions.base import Refused
from aipanel.auth import csrf_token
from aipanel.config.store import SettingsError
from aipanel.runner import Job
from aipanel.web.deps import csrf_guard, panel_of, user_of

router = APIRouter(prefix="/api", dependencies=[Depends(csrf_guard)])


class StartAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    params: dict[str, Any] = Field(default_factory=dict)
    confirm: str | None = None


class Hold(BaseModel):
    kind: Literal["on", "off"]
    until: datetime


class BoundaryRef(BaseModel):
    id: str


def _job(job: Job | None) -> Any:
    return jsonable_encoder(job) if job is not None else None


@router.get("/session")
async def session(request: Request) -> dict[str, Any]:
    user = user_of(request)
    return {
        "user": user.name,
        "groups": list(user.groups),
        "csrf": csrf_token(panel_of(request).csrf_key, user),
    }


@router.get("/status")
async def status(request: Request) -> Any:
    panel = panel_of(request)
    upcoming = panel.scheduler.upcoming(days=2)
    return jsonable_encoder(
        {
            "snapshot": panel.poller.snapshot,
            "job": panel.runner.current,
            "last_job": panel.runner.history[0] if panel.runner.history else None,
            "next_event": (
                {
                    "id": upcoming[0][0].id,
                    "kind": upcoming[0][0].kind,
                    "at": upcoming[0][0].at,
                    "note": upcoming[0][1],
                }
                if upcoming
                else None
            ),
            "cost_now": panel.cost.now(panel.poller.snapshot),
            "config_valid": panel.loader.valid,
            "config_error": panel.loader.error,
            "dry_run": panel.site.actions.dry_run,
            "demo": panel.demo,
        }
    )


@router.get("/actions")
async def actions(request: Request) -> list[dict[str, Any]]:
    return [
        {
            "name": a.name,
            "label": a.label,
            "group": a.group,
            "confirm": a.confirm.value,
            "warning": a.warning,
            "params": a.params.model_json_schema(),
        }
        for a in registry.enabled(panel_of(request).site)
    ]


@router.post("/actions/{name}", status_code=202)
async def start(request: Request, name: str, body: StartAction) -> Any:
    panel = panel_of(request)
    try:
        job = await panel.runner.start(name, user_of(request).name, body.params, body.confirm)
    except Refused as e:
        return JSONResponse({"error": str(e), "retryable": e.retryable}, status_code=409)
    return _job(job)


@router.get("/jobs/{job_id}")
async def job(request: Request, job_id: str) -> Any:
    found = panel_of(request).runner.job(job_id)
    if found is None:
        raise HTTPException(404, "no such job")
    return _job(found)


@router.get("/schedule")
async def schedule(request: Request) -> Any:
    panel = panel_of(request)
    return jsonable_encoder(
        {
            "settings": panel.store.get().schedule,
            "state": panel.scheduler.state,
            "upcoming": [
                {"id": b.id, "rule": b.rule, "kind": b.kind, "at": b.at, "note": note}
                for b, note in panel.scheduler.upcoming(days=7)
            ],
            "protected_windows": panel.site.protected_windows,
        }
    )


@router.post("/schedule/skip")
async def skip(request: Request, body: BoundaryRef) -> dict[str, str]:
    try:
        panel_of(request).scheduler.skip(body.id, user_of(request).name)
    except ValueError as e:
        raise HTTPException(404, str(e)) from None
    return {"skipped": body.id}


@router.post("/schedule/unskip")
async def unskip(request: Request, body: BoundaryRef) -> dict[str, str]:
    panel_of(request).scheduler.unskip(body.id, user_of(request).name)
    return {"unskipped": body.id}


@router.post("/schedule/hold")
async def hold(request: Request, body: Hold) -> dict[str, str]:
    until = (
        body.until
        if body.until.tzinfo
        else body.until.replace(tzinfo=panel_of(request).site.site.tz)
    )
    try:
        panel_of(request).scheduler.hold(body.kind, until, user_of(request).name)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return {"held": body.kind, "until": until.isoformat()}


@router.delete("/schedule/hold")
async def clear_hold(request: Request) -> dict[str, bool]:
    panel_of(request).scheduler.clear_holds(user_of(request).name)
    return {"cleared": True}


@router.get("/cost")
async def cost(request: Request, refresh: bool = False) -> Any:
    panel = panel_of(request)
    return jsonable_encoder(await panel.cost.report(panel.poller.snapshot, refresh=refresh))


@router.get("/settings")
async def get_settings(request: Request) -> Any:
    panel = panel_of(request)
    return {
        "settings": panel.store.get().model_dump(mode="json"),
        "error": panel.store.error,
        "conflict": panel.store.conflict(),
        "writable": panel.store.writable,
    }


async def _apply(request: Request, body: dict[str, Any], replace: bool) -> Any:
    panel, user = panel_of(request), user_of(request)
    try:
        write = panel.store.replace if replace else panel.store.update
        settings = await asyncio.to_thread(write, body, user.name)  # off the event loop
    except SettingsError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    return settings.model_dump(mode="json")


@router.patch("/settings")
async def patch_settings(request: Request, body: dict[str, Any]) -> Any:
    """Change some settings: nested objects, or dotted keys ({"schedule.enabled": true})."""
    return await _apply(request, body, replace=False)


@router.put("/settings")
async def put_settings(request: Request, body: dict[str, Any]) -> Any:
    """Replace all settings; what is not given returns to its default."""
    return await _apply(request, body, replace=True)


@router.get("/audit")
async def audit(request: Request, limit: int = 100, q: str = "", actor: str = "") -> Any:
    return panel_of(request).audit.read(min(max(limit, 1), 2000), q, actor)
