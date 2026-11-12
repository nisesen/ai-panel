"""What the page and the API routes share: the panel, the user, the CSRF check."""

from fastapi import HTTPException, Request

from aipanel.auth import CSRF_FIELD, CSRF_HEADER, User, csrf_ok
from aipanel.panel import Panel

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def panel_of(request: Request) -> Panel:
    panel: Panel = request.app.state.panel
    return panel


def user_of(request: Request) -> User:
    user: User = request.state.user
    return user


async def csrf_guard(request: Request) -> None:
    """Every change carries the CSRF token, as a header (htmx, the API) or a form field."""
    if request.method in SAFE_METHODS:
        return
    token = request.headers.get(CSRF_HEADER)
    if token is None and "form" in request.headers.get("content-type", ""):
        value = (await request.form()).get(CSRF_FIELD)
        token = value if isinstance(value, str) else None
    if not csrf_ok(panel_of(request).csrf_key, user_of(request), token):
        raise HTTPException(403, "missing or stale CSRF token: reload the page")
