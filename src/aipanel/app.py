"""The two web apps: the panel (pages and JSON API) and, on its own port, /metrics and /healthz."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aipanel import __version__
from aipanel.api.routes import router as api_router
from aipanel.auth import AuthError, authenticate
from aipanel.panel import Panel
from aipanel.web.routes import router as web_router

STATIC = Path(__file__).parent / "web" / "static"
MAX_BODY = 64 * 1024  # the largest settings import is a few kilobytes

# Scripts and styles only from the panel itself; htmx is configured not to evaluate code.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


class BodyLimit:
    """Refuse request bodies over MAX_BODY, declared or streamed."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"0")
        if not declared.isdigit() or int(declared) > MAX_BODY:
            await PlainTextResponse("request body too large", 413)(scope, receive, send)
            return
        seen = 0

        async def limited() -> Message:
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > MAX_BODY:
                raise HTTPException(413, "request body too large")
            return message

        await self.app(scope, limited, send)


def create_app(panel: Panel, run_background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if run_background:
            await panel.start()
        yield
        if run_background:
            await panel.stop()

    app = FastAPI(
        title="ai-panel",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.panel = panel

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        client = request.client.host if request.client else None
        try:
            request.state.user = authenticate(headers, client, panel.site.auth, panel.demo)
        except AuthError as e:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": str(e)}, status_code=e.status)
            return PlainTextResponse(str(e), status_code=e.status)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.add_middleware(BodyLimit)
    app.include_router(api_router)
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def create_ops_app(panel: Panel) -> FastAPI:
    """Not routed by the gateway: for Prometheus and the kubelet."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        panel.update_status_metrics()
        return PlainTextResponse(panel.metrics.render(), media_type="text/plain; version=0.0.4")

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {
            "ok": True,
            "version": __version__,
            "config_valid": panel.loader.valid,
            "state_writable": panel.state_writable,
        }

    return app
