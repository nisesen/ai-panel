"""The pages and the API through HTTP: sign-in by trusted headers (no header, no access), the
CSRF token on every change, and the forms."""

import logging
import re
import stat
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from conftest import MakePanel, site_config

from aipanel.app import create_app, create_ops_app
from aipanel.auth import AuthError, User, authenticate, csrf_token, load_csrf_key
from aipanel.config.site import AuthConfig
from aipanel.panel import Panel

pytestmark = pytest.mark.anyio

AUTH = {
    "mode": "trusted_header",
    "user_header": "X-Forwarded-User",
    "groups_header": "X-Forwarded-Groups",
    "groups_separator": "|",
    "allowed_groups": ["admins"],
    "trusted_proxies": ["127.0.0.1/32"],  # the test client's address
}
ALICE = {"X-Forwarded-User": "alice", "X-Forwarded-Groups": "users|admins"}


def client(panel: Panel, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    app = create_app(panel, run_background=False)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://panel", headers=headers or {}
    )


@pytest.fixture
async def secured(make_panel: MakePanel) -> AsyncIterator[Panel]:
    panel = make_panel(site_config(auth=AUTH))
    await panel.poller.refresh()
    yield panel


async def csrf(c: httpx.AsyncClient) -> str:
    return str((await c.get("/api/session")).json()["csrf"])


# Sign-in


@pytest.mark.parametrize("path", ["/", "/settings", "/api/status", "/static/style.css"])
async def test_no_header_no_access(secured: Panel, path: str) -> None:
    async with client(secured) as c:
        response = await c.get(path)
    assert response.status_code == 401


async def test_a_user_outside_the_allowed_groups_is_refused(secured: Panel) -> None:
    async with client(secured, {"X-Forwarded-User": "eve", "X-Forwarded-Groups": "users"}) as c:
        assert (await c.get("/")).status_code == 403


async def test_a_signed_in_admin_sees_every_page(secured: Panel) -> None:
    async with client(secured, ALICE) as c:
        for path in (
            "/",
            "/schedule",
            "/cost",
            "/activity",
            "/settings",
            "/fragments/status",
            "/fragments/job",
            "/api/status",
            "/api/cost",
            "/api/schedule",
        ):
            response = await c.get(path)
            assert response.status_code == 200, path
        page = await c.get("/")
    assert ">alice<" in page.text and "gpu-demo" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]


async def test_trusted_proxies(make_panel: MakePanel) -> None:
    panel = make_panel(site_config(auth={**AUTH, "trusted_proxies": ["192.0.2.0/24"]}))
    async with client(panel, ALICE) as c:  # the test client comes from 127.0.0.1
        assert (await c.get("/")).status_code == 403


def test_auth_none_needs_demo_mode() -> None:
    with pytest.raises(AuthError):
        authenticate({}, None, AuthConfig(mode="none"), demo=False)
    assert authenticate({}, None, AuthConfig(mode="none"), demo=True).name == "demo"


# CSRF


async def test_changes_need_the_csrf_token(secured: Panel) -> None:
    async with client(secured, ALICE) as c:
        assert (await c.post("/actions/probe")).status_code == 403
        assert (await c.post("/actions/probe", data={"csrf": "0" * 64})).status_code == 403
        assert (
            await c.patch("/api/settings", json={"poll.interval_seconds": 20})
        ).status_code == 403
        token = await csrf(c)
        response = await c.post("/actions/probe", data={"csrf": token})
        assert response.status_code == 303
    async with client(secured, {**ALICE, "X-Forwarded-User": "eve"}) as c:  # someone else's token
        assert (await c.post("/actions/probe", data={"csrf": token})).status_code == 403


def test_the_csrf_key_survives_a_restart(make_panel: MakePanel, tmp_path: Path) -> None:
    first = make_panel()
    key = first.state_dir / "csrf.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600 and len(key.read_text().strip()) == 64
    again = make_panel()  # a restart: same state directory
    user = User("alice")
    assert csrf_token(again.csrf_key, user) == csrf_token(first.csrf_key, user)


def test_a_broken_csrf_key_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "csrf.key"
    path.write_text("not hex\n")
    key = load_csrf_key(path)
    assert path.read_text().strip() == key.hex() and stat.S_IMODE(path.stat().st_mode) == 0o600


def test_csrf_key_in_memory_when_the_state_is_read_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = tmp_path / "ro"
    directory.mkdir()
    directory.chmod(0o500)
    try:
        with caplog.at_level(logging.WARNING, logger="aipanel.auth"):
            one, two = load_csrf_key(directory / "csrf.key"), load_csrf_key(directory / "csrf.key")
    finally:
        directory.chmod(0o700)
    assert len(one) == 32 and one != two and not (directory / "csrf.key").exists()
    assert "kept in memory" in caplog.text


# Forms


async def test_starting_an_action_from_the_page(secured: Panel) -> None:
    async with client(secured, ALICE) as c:
        token = await csrf(c)
        response = await c.post("/actions/shutdown", data={"csrf": token, "confirm": "wrong"})
        assert response.status_code == 303
        page = await c.get("/")
        assert "Not started: Shut down needs confirmation" in page.text
        await c.post("/actions/llm_switch", data={"csrf": token, "model": "moe"})
        await secured.runner.wait()
        page = await c.get("/fragments/job")
    assert "Switch model" in page.text and "succeeded" in page.text
    assert secured.audit.read(1, "action.finished")[0]["actor"] == "alice"


async def test_the_settings_form(secured: Panel) -> None:
    async with client(secured, ALICE) as c:
        token = await csrf(c)
        page = await c.get("/settings")
        assert 'name="poll.interval_seconds"' in page.text and 'min="5"' in page.text
        form = {
            "csrf": token,
            "poll.interval_seconds": "20",
            "cost.idle_watts": "",
            "display.theme": "dark",
            "schedule.force_after_grace": "on",
        }
        assert (await c.post("/settings", data=form)).status_code == 303
        settings = secured.store.get()
        assert settings.poll.interval_seconds == 20 and settings.display.theme == "dark"
        assert settings.schedule.force_after_grace and not settings.schedule.enabled
        assert 'data-theme="dark"' in (await c.get("/")).text
        await c.post("/settings", data={**form, "poll.interval_seconds": "1"})
        assert "Not saved" in (await c.get("/settings")).text
        assert secured.store.get().poll.interval_seconds == 20


async def test_the_schedule_page(secured: Panel) -> None:
    async with client(secured, ALICE) as c:
        token = await csrf(c)
        rule = {
            "csrf": token,
            "name": "nights",
            "days": ["mon", "tue"],
            "off": "23:00",
            "on": "07:00",
        }
        await c.post("/schedule/rules", data=rule)
        await c.post("/schedule/rules", data={**rule, "name": "evening", "off": "21:30"})
        page = await c.get("/schedule")
        assert "overlaps the protected window" in page.text
        await c.post("/schedule/enabled", data={"csrf": token, "enabled": "true"})
        page = await c.get("/schedule")
        assert "nights" in page.text and '<rect class="off"' in page.text
        boundary = re.search(r'name="boundary" value="([^"]+)"', page.text)
        assert boundary
        await c.post("/schedule/skip", data={"csrf": token, "boundary": boundary.group(1)})
    assert [r.name for r in secured.store.get().schedule.rules] == ["nights"]
    assert secured.store.get().schedule.enabled and secured.scheduler.state.skip


# The JSON API


async def test_api(secured: Panel) -> None:
    async with client(secured, ALICE) as c:
        headers = {"X-CSRF-Token": await csrf(c)}
        started = await c.post("/api/actions/probe", json={}, headers=headers)
        assert started.status_code == 202 and started.json()["action"] == "probe"
        await secured.runner.wait()
        job = await c.get(f"/api/jobs/{started.json()['id']}")
        assert job.json()["status"] == "succeeded"
        refused = await c.post("/api/actions/power_on", json={}, headers=headers)
        assert refused.status_code == 409 and "on already" in refused.json()["error"]
        changed = await c.patch(
            "/api/settings", json={"poll.interval_seconds": 30}, headers=headers
        )
        assert changed.status_code == 200 and changed.json()["poll"]["interval_seconds"] == 30
        bad = await c.patch("/api/settings", json={"poll.interval_seconds": 3}, headers=headers)
        assert bad.status_code == 422
        actions = (await c.get("/api/actions")).json()
        assert {a["name"] for a in actions} >= {"power_on", "shutdown", "llm_switch"}
        audit = (await c.get("/api/audit", params={"actor": "alice"})).json()
    assert audit and all(e["actor"] == "alice" for e in audit)


# The operations port


async def test_metrics_and_healthz(panel: Panel) -> None:
    await panel.poller.refresh()
    app = create_ops_app(panel)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ops"
    ) as c:
        metrics = (await c.get("/metrics")).text
        health = (await c.get("/healthz")).json()
    assert (
        "aipanel_config_valid 1.0" in metrics and 'aipanel_backend_up{backend="bmc"} 1.0' in metrics
    )
    assert health["ok"] and health["config_valid"]
