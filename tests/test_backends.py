"""The real backends, against fake commands and fake HTTP servers: what they send, and how
they read the answers and the failures."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from conftest import site_config

from aipanel.backends.agent import SshAgent
from aipanel.backends.llm import LlamaCpp
from aipanel.backends.metrics import Prometheus
from aipanel.backends.power import IpmiPower, parse_sdr
from aipanel.backends.process import CommandTimeout, Result, run
from aipanel.backends.protocols import BackendError, Unreachable
from aipanel.backends.silences import Alertmanager
from aipanel.config.site import BmcConfig

pytestmark = pytest.mark.anyio
SITE = site_config()


class FakeRun:
    def __init__(self, *results: Result | Exception) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    async def __call__(
        self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None
    ) -> Result:
        self.calls.append((list(argv), dict(env or {})))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


# BMC


def bmc(password: dict[str, str]) -> BmcConfig:
    return BmcConfig.model_validate(
        {"host": "192.0.2.10", "username": {"env": "BMC_USER"}, "password": password}
    )


async def test_ipmitool_gets_the_password_from_a_file_or_its_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BMC_USER", "operator")
    monkeypatch.setenv("BMC_PASSWORD", "hunter2")
    fake = FakeRun(Result(0, "Chassis Power is on\n", ""), Result(0, "Chassis Power is off\n", ""))
    assert await IpmiPower(bmc({"file": str(tmp_path / "pw")}), fake).status() == "on"
    assert await IpmiPower(bmc({"env": "BMC_PASSWORD"}), fake).status() == "off"
    (with_file, _), (with_env, env) = fake.calls
    assert with_file[:3] == ["ipmitool", "-I", "lanplus"] and "-f" in with_file
    assert with_file[7:13] == ["-U", "operator", "-L", "OPERATOR", "-C", "3"]
    assert with_file[-3:] == ["chassis", "power", "status"]
    assert "-E" in with_env and env["IPMI_PASSWORD"] == "hunter2"
    assert all("hunter2" not in arg for arg in with_file + with_env)


async def test_bmc_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BMC_USER", "operator")
    monkeypatch.setenv("BMC_PASSWORD", "x")
    power = IpmiPower(
        bmc({"env": "BMC_PASSWORD"}),
        FakeRun(
            CommandTimeout("ipmitool did not finish"),
            Result(1, "", "Error: Unable to establish IPMI v2 / RMCP+ session\n"),
            Result(1, "", "Set Chassis Power Control to Up/On failed: Insufficient privilege\n"),
        ),
    )
    with pytest.raises(Unreachable):
        await power.status()
    with pytest.raises(Unreachable, match="Unable to establish"):
        await power.on()
    with pytest.raises(BackendError, match="Insufficient privilege"):
        await power.on()


def test_sdr_parsing() -> None:
    text = "CPU Temp | 45 degrees C | ok\nFAN1 | no reading | ns\nPSU | 0x01 | ok\n"
    assert [s.name for s in parse_sdr(text)] == ["CPU Temp", "PSU"]


# The host agent over SSH


async def test_ssh_options_and_reply() -> None:
    fake = FakeRun(Result(0, json.dumps({"ok": True, "verb": "status"}), ""))
    agent = SshAgent(SITE.host, fake)
    assert (await agent.call(["status"], 30))["verb"] == "status"
    argv, _env = fake.calls[0]
    assert argv[0] == "ssh" and argv[-2:] == ["192.0.2.20", "status"]
    for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "IdentitiesOnly=yes"):
        assert option in argv
    assert argv[1:3] == ["-F", "none"] and argv[7:9] == ["-l", "llmctl"]


async def test_ssh_refuses_to_send_anything_but_words() -> None:
    agent = SshAgent(SITE.host, FakeRun())
    for bad in (["status;", "id"], ["$(id)"], [], ["-oProxyCommand=x"]):
        with pytest.raises(BackendError, match="refusing"):
            await agent.call(bad, 30)


async def test_ssh_failures() -> None:
    agent = SshAgent(
        SITE.host,
        FakeRun(
            Result(255, "", "ssh: connect to host 192.0.2.20 port 22: Connection timed out\n"),
            Result(2, json.dumps({"ok": False, "error": "unknown model 'x'"}), ""),
            Result(0, "Welcome to the shell", ""),
            CommandTimeout("ssh did not finish"),
        ),
    )
    with pytest.raises(Unreachable, match="timed out"):
        await agent.call(["status"], 30)
    with pytest.raises(BackendError, match="unknown model"):
        await agent.call(["llm", "switch", "x"], 30)
    with pytest.raises(BackendError, match="not a JSON answer"):
        await agent.call(["status"], 30)
    with pytest.raises(Unreachable):
        await agent.call(["status"], 30)


async def test_run_never_uses_a_shell() -> None:
    result = await run(["echo", "$HOME;", "`id`"], timeout=5)
    assert result.stdout == "$HOME; `id`\n"
    with pytest.raises(CommandTimeout):
        await run(["sleep", "5"], timeout=0.1)


# HTTP backends


def http(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def test_llama_cpp(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("k-123\n")
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        match request.url.path:
            case "/health":
                return httpx.Response(200, json={"status": "ok"})
            case "/props":
                return httpx.Response(200, json={"model_alias": "m", "model_path": "/m.gguf"})
            case _:
                return httpx.Response(200, json=[{"is_processing": True}, {"is_processing": False}])

    config = type(SITE.llm).model_validate({**SITE.llm.model_dump(), "api_key": {"file": key}})
    llm = LlamaCpp(config, http(httpx.MockTransport(handle)))
    assert await llm.health() and (await llm.props())["model_alias"] == "m"
    assert await llm.slots() == (1, 2)
    assert seen[1].headers["authorization"] == "Bearer k-123"


async def test_llama_cpp_down() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(Unreachable):
        await LlamaCpp(SITE.llm, http(httpx.MockTransport(handle))).health()


async def test_prometheus() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/query":
            result = [{"metric": {"gpu": "0"}, "value": [1, "41.5"]}]
            return httpx.Response(
                200, json={"status": "success", "data": {"resultType": "vector", "result": result}}
            )
        if request.url.params["query"] == "bad(":
            return httpx.Response(400, json={"status": "error", "error": "parse error"})
        result = [{"metric": {}, "values": [[1, "1"], [301, "2"]]}]
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": result}}
        )

    prom = Prometheus(SITE.prometheus, http(httpx.MockTransport(handle)))  # type: ignore[arg-type]
    assert (await prom.query("up"))[0].value == 41.5
    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    assert (await prom.query_range("up", start, end, 300))[0].points == [(1.0, 1.0), (301.0, 2.0)]
    with pytest.raises(BackendError, match="parse error"):
        await prom.query_range("bad(", start, end, 300)


async def test_alertmanager() -> None:
    posted: list[dict[str, object]] = []
    deleted: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(json.loads(request.content))
            return httpx.Response(200, json={"silenceID": "abc"})
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(200)
        silences = [
            {
                "id": "abc",
                "createdBy": "ai-panel",
                "status": {"state": "active"},
                "endsAt": "2026-01-01T08:00:00Z",
                "comment": "x",
            },
            {
                "id": "other",
                "createdBy": "someone",
                "status": {"state": "active"},
                "endsAt": "2026-01-01T08:00:00Z",
            },
        ]
        return httpx.Response(200, json=silences)

    am = Alertmanager(SITE.alertmanager, http(httpx.MockTransport(handle)))  # type: ignore[arg-type]
    assert await am.create(datetime(2026, 1, 1, 8, tzinfo=UTC), "planned") == "abc"
    assert posted[0]["matchers"] == [
        {"name": "host", "value": "gpu-demo", "isRegex": False, "isEqual": True}
    ]
    assert [s.id for s in await am.active()] == ["abc"]  # only the panel's own
    await am.expire("abc")
    assert deleted == ["/api/v2/silence/abc"]


def test_vendored_htmx_is_the_recorded_release() -> None:
    path = Path(__file__).parents[1] / "src/aipanel/web/static/htmx.min.js"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"
