"""llm-ctl: the verbs it accepts, the ones it refuses, and the exact commands it runs.

Loaded from its file (it has no .py extension), and driven with a fake command runner.
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import sys
import syslog
import time
from pathlib import Path

import pytest

PATH = Path(__file__).parents[1] / "llm-ctl"
_loader = importlib.machinery.SourceFileLoader("llm_ctl", str(PATH))
_spec = importlib.util.spec_from_loader("llm_ctl", _loader)
assert _spec is not None
llm_ctl = importlib.util.module_from_spec(_spec)
_loader.exec_module(llm_ctl)

CONFIG = """
node = "gpu-host"
[llm]
ct = 210
url = "http://127.0.0.1:8080"
api_key_file = "/etc/llama/api-key"
default_model = "dense"
[llm.models.dense]
unit = "llama-server"
[llm.models.moe]
unit = "llama-moe"
[guests]
allowed = [210, 211, 212]
[client_checks]
vmid = 212
pause = ["/usr/bin/systemctl", "--user", "--machine=agent@", "stop", "llm-check.timer"]
resume = ["/usr/bin/systemctl", "--user", "--machine=agent@", "start", "llm-check.timer"]
"""
CT = ("/usr/sbin/pct", "exec", "210", "--")
SHOW = (*CT, "/usr/bin/systemctl", "show")
UNITS = "LoadState=loaded\nActiveState=active\n\nLoadState=loaded\nActiveState=inactive\n"

GUESTS_LXC = json.dumps(
    [
        {"vmid": 210, "name": "llm", "status": "running"},
        {"vmid": 211, "name": "dash", "status": "running"},
        {"vmid": 215, "name": "other", "status": "running"},
    ]
)
GUESTS_QEMU = json.dumps([{"vmid": 212, "name": "agent", "status": "running"}])
SECRET_KEY = "sk-test-not-a-real-key"


class FakeRunner:
    """Answers by the longest matching argv prefix; records every call."""

    def __init__(self, answers=None):
        self.calls = []
        self.inputs = []
        self.answers = {
            ("/usr/bin/pvesh", "get", "/nodes/gpu-host/lxc"): (0, GUESTS_LXC),
            ("/usr/bin/pvesh", "get", "/nodes/gpu-host/qemu"): (0, GUESTS_QEMU),
            ("/usr/bin/pvesh", "get", "/nodes/gpu-host/tasks"): (0, "[]"),
            SHOW: (0, UNITS),
            (*CT, "/usr/bin/curl", "-s", "-o"): (0, "200"),
            (*CT, "/usr/bin/cat"): (0, SECRET_KEY + "\n"),
            (*CT, "/usr/bin/curl", "-s", "-m"): (
                0,
                json.dumps({"model_alias": "dense-32b", "model_path": "/m/dense.gguf"}),
            ),
        }
        self.answers.update(answers or {})
        self.on_call = None

    def run(self, argv, input=None, timeout=60):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        self.calls.append(argv)
        self.inputs.append(input)
        if self.on_call:
            self.on_call(argv)
        best = None
        for prefix, answer in self.answers.items():
            if tuple(argv[: len(prefix)]) == prefix and (best is None or len(prefix) > best[0]):
                best = (len(prefix), answer)
        code, out = best[1] if best else (0, "")
        return llm_ctl.Result(code, out, "" if code == 0 else "boom")

    def ran(self, *prefix):
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]


@pytest.fixture
def env(tmp_path):
    folder = tmp_path / "etc"
    folder.mkdir(mode=0o755)
    path = folder / "config.toml"
    path.write_text(f'lock_file = "{tmp_path / "llm-ctl.lock"}"\n' + CONFIG)
    path.chmod(0o644)
    return {"CONFIG": str(path), "SSH_CLIENT": "192.0.2.50 40000 22"}


def call(env, words, runner=None, euid=0, argv=None, uptime="3600.5 100", out=None):
    out, logs, execs = out or io.StringIO(), [], []
    environ = {k: v for k, v in env.items() if k != "CONFIG"}
    if words is not None:
        environ["SSH_ORIGINAL_COMMAND"] = words
    code = llm_ctl.main(
        config_path=env["CONFIG"],
        owner=os.getuid(),
        argv=argv or [],
        environ=environ,
        runner=runner or FakeRunner(),
        out=out,
        log=lambda prio, msg: logs.append((prio, msg)),
        euid=euid,
        execv=lambda path, args: execs.append(args),
        read_file=lambda p: {
            "/proc/uptime": uptime,
            "/proc/sys/kernel/random/boot_id": "b1\n",
        }[p],
    )
    text = out.getvalue().strip() if isinstance(out, io.StringIO) else ""
    return code, json.loads(text) if text else None, logs, execs


@pytest.fixture
def config(env):
    return llm_ctl.load_config(env["CONFIG"])


# Parsing and refusals


@pytest.mark.parametrize(
    "words",
    [
        "status",
        "shutdown",
        "reboot",
        "probe",
        "llm switch moe",
        "llm restart",
        "llm stop",
        "llm start",
        "guest 211 shutdown",
        "guest 212 reboot",
        "client-checks pause",
    ],
)
def test_accepts_the_listed_verbs(config, words):
    assert llm_ctl.parse(words.split(), config) == tuple(words.split())


@pytest.mark.parametrize(
    ("words", "reason"),
    [
        ("", "no command"),
        ("rm -rf /", "refused word"),
        ("status; id", "refused word"),
        ("status $(id)", "refused word"),
        ("status`id`", "refused word"),
        ("status extra", "unknown command"),
        ("llm switch other", "unknown model"),
        ("llm switch", "unknown command"),
        ("llm kill", "unknown command"),
        ("guest 215 start", "not allowed"),
        ("guest abc start", "not allowed"),
        ("guest 211 destroy", "unknown command"),
        ("bash", "unknown command"),
        ("../../bin/sh", "refused word"),
    ],
)
def test_refuses_everything_else(config, words, reason):
    with pytest.raises(llm_ctl.Refused, match=reason):
        llm_ctl.parse(words.split(), config)


def test_client_checks_refused_when_not_configured(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(CONFIG.split("[client_checks]")[0])
    with pytest.raises(llm_ctl.Refused, match="not configured"):
        llm_ctl.parse(["client-checks", "pause"], llm_ctl.load_config(path))


def test_refusal_answers_json_logs_and_exits_2(env):
    runner = FakeRunner()
    code, reply, logs, execs = call(env, "status; reboot", runner, euid=1001)
    assert code == 2 and reply["ok"] is False
    assert execs == [] and runner.calls == []
    assert logs[0][0] == syslog.LOG_WARNING and "192.0.2.50" in logs[0][1]


def test_bad_config_answers_an_error(env):
    pathlib.Path(env["CONFIG"]).write_text("[llm]\n")
    code, reply, _, _ = call(env, "status")
    assert code == 1 and "llm.ct" in reply["error"]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ('[commands]\nbash = ["/bin/bash"]\n', "commands.bash"),
        ('[commands]\npct = ["pct"]\n', "absolute path"),
    ],
)
def test_config_command_rules(env, extra, message):
    path = pathlib.Path(env["CONFIG"])
    path.write_text(path.read_text() + "\n" + extra)
    code, reply, _, _ = call(env, "status")
    assert code == 1 and message in reply["error"]


@pytest.mark.parametrize("target", ["file", "folder"])
def test_a_writable_config_is_refused(env, target):
    path = pathlib.Path(env["CONFIG"])
    (path if target == "file" else path.parent).chmod(0o777 if target == "folder" else 0o666)
    code, reply, _, _ = call(env, "status")
    assert code == 1 and "not group/world-writable" in reply["error"]


def test_root_reads_only_its_own_config_path():
    import inspect

    assert inspect.signature(llm_ctl.main).parameters["config_path"].default == llm_ctl.CONFIG
    assert "LLM_CTL_CONFIG" not in PATH.read_text()


# Unprivileged: check, then sudo itself with the checked words


def test_unprivileged_reexecs_through_sudo(env):
    _, _, logs, execs = call(env, "llm switch moe", euid=1001)
    assert execs == [["/usr/bin/sudo", "-n", "/usr/local/sbin/llm-ctl", "llm", "switch", "moe"]]
    assert "192.0.2.50" in logs[0][1]


def test_root_takes_the_words_from_its_arguments(env):
    runner = FakeRunner()
    code, reply, _, _ = call(env, None, runner, argv=["llm", "restart"])
    assert code == 0 and reply["verb"] == "llm restart"


# The verbs


def test_status(env):
    code, reply, logs, _ = call(env, "status")
    assert code == 0 and reply["ok"] is True
    assert reply["host"]["uptime_seconds"] == 3600 and reply["host"]["boot_id"] == "b1"
    assert [g["id"] for g in reply["guests"]] == [210, 211, 212]  # 215 is not listed
    assert reply["guests"][2]["type"] == "qemu"
    assert reply["llm"]["active_model"] == "dense"
    assert reply["llm"]["units"]["moe"] == {"active": "inactive", "load": "loaded"}
    assert reply["llm"]["health"] == 200 and reply["llm"]["model_alias"] == "dense-32b"
    assert logs[-1][0] == syslog.LOG_DEBUG


def test_status_reads_props_with_the_key_on_stdin_only(env):
    runner = FakeRunner()
    call(env, "status", runner)
    assert all(SECRET_KEY not in " ".join(c) for c in runner.calls)
    assert f"Authorization: Bearer {SECRET_KEY}\n" in runner.inputs
    props = runner.ran(*CT, "/usr/bin/curl", "-s", "-m")[0]
    assert props[-3:] == ["-H", "@-", "http://127.0.0.1:8080/props"]


def test_untrusted_props_become_short_strings(env):
    huge = {"model_alias": "x" * 10_000, "model_path": {"nested": [1]}, "other": "y" * 10_000}
    runner = FakeRunner({(*CT, "/usr/bin/curl", "-s", "-m"): (0, json.dumps(huge))})
    reply = call(env, "status", runner)[1]
    assert len(reply["llm"]["model_alias"]) == 200 and reply["llm"]["model_path"] is None
    assert "other" not in reply["llm"]
    deep = FakeRunner({(*CT, "/usr/bin/curl", "-s", "-m"): (0, "[" * 100_000)})
    assert call(env, "status", deep)[0] == 0  # too deep to parse: no props, no crash


def test_shutdown_stops_the_llm_then_answers_then_powers_off(env):
    runner = FakeRunner()
    out = io.StringIO()
    seen = []
    runner.on_call = lambda argv: seen.append(out.getvalue()) if "poweroff" in argv else None
    code = call(env, "shutdown", runner, out=out)[0]
    assert code == 0
    assert runner.calls[0] == ["/usr/sbin/pct", "shutdown", "210", "--timeout", "120"]
    assert runner.calls[-1] == ["/usr/bin/systemctl", "poweroff"]
    assert '"ok":true' in seen[0]  # the answer was out before the power went


def test_shutdown_powers_off_even_when_the_panel_is_gone(env):
    class Gone(io.StringIO):
        def write(self, text):
            raise BrokenPipeError(32, "Broken pipe")

    runner = FakeRunner()
    assert call(env, "shutdown", runner, out=Gone())[0] == 0
    assert runner.calls[-1] == ["/usr/bin/systemctl", "poweroff"]


def test_no_shutdown_or_reboot_right_after_a_boot(env):
    for verb in ("shutdown", "reboot"):
        runner = FakeRunner()
        code, reply, _, _ = call(env, verb, runner, uptime="120.0 10")
        assert code == 1 and "started 2 min ago" in reply["error"] and runner.calls == []


def test_shutdown_fails_when_the_llm_guest_does_not_stop(env):
    runner = FakeRunner(
        {
            ("/usr/sbin/pct", "shutdown"): (1, ""),
            ("/usr/sbin/pct", "status"): (0, "status: running"),
        }
    )
    code, reply, _, _ = call(env, "shutdown", runner)
    assert code == 1 and "did not stop" in reply["error"]
    assert not runner.ran("/usr/bin/systemctl", "poweroff")


def test_reboot(env):
    runner = FakeRunner()
    code = call(env, "reboot", runner)[0]
    assert code == 0 and runner.calls[-1] == ["/usr/bin/systemctl", "reboot"]


def test_one_changing_verb_at_a_time(env):
    held = llm_ctl._lock(llm_ctl.load_config(env["CONFIG"]).lock)
    try:
        code, reply, _, _ = call(env, "llm restart")
        assert code == 1 and reply["error"].startswith("busy")
        assert call(env, "status")[0] == 0  # reading is never locked out
    finally:
        os.close(held)


def test_llm_switch(env):
    after = "LoadState=loaded\nActiveState=inactive\n\nLoadState=loaded\nActiveState=active\n"
    runner = FakeRunner({SHOW: (0, after)})
    code, reply, _, _ = call(env, "llm switch moe", runner)
    assert code == 0 and reply["unit"] == "llama-moe"
    ctl = [*CT, "/usr/bin/systemctl"]
    assert runner.calls[:3] == [
        [*ctl, "stop", "llama-server"],
        [*ctl, "reset-failed", "llama-server", "llama-moe"],
        [*ctl, "start", "llama-moe"],
    ]
    assert runner.calls[3][: len(SHOW)] == list(SHOW)  # checked afterwards


def test_llm_switch_goes_on_after_a_failed_stop_and_checks_the_result(env):
    runner = FakeRunner({(*CT, "/usr/bin/systemctl", "stop"): (1, "")})  # both stay "active"
    runner.answers[SHOW] = (0, UNITS.replace("inactive", "active"))
    code, reply, _, _ = call(env, "llm switch moe", runner)
    assert code == 1 and "still running beside moe: dense" in reply["error"]
    assert runner.ran(*CT, "/usr/bin/systemctl", "start")


def test_llm_switch_reports_a_missing_unit(env):
    gone = "LoadState=loaded\nActiveState=inactive\n\nLoadState=not-found\nActiveState=inactive\n"
    code, reply, _, _ = call(env, "llm switch moe", FakeRunner({SHOW: (0, gone)}))
    assert code == 1 and "not-found" in reply["error"]


def test_llm_restart_restarts_the_active_unit(env):
    runner = FakeRunner()
    code, reply, _, _ = call(env, "llm restart", runner)
    assert code == 0 and reply["model"] == "dense"
    assert runner.calls[-1][-2:] == ["restart", "llama-server"]


def test_llm_restart_fails_without_an_active_unit(env):
    runner = FakeRunner({SHOW: (0, UNITS.replace("=active", "=inactive"))})
    code, reply, _, _ = call(env, "llm restart", runner)
    assert code == 1 and "no model" in reply["error"]


def test_llm_start_starts_the_default_when_none_runs(env):
    runner = FakeRunner({SHOW: (0, UNITS.replace("=active", "=failed"))})
    code = call(env, "llm start", runner)[0]
    assert code == 0 and runner.calls[-1][-2:] == ["start", "llama-server"]


def test_llm_stop_stops_every_unit(env):
    runner = FakeRunner()
    call(env, "llm stop", runner)
    assert runner.calls[-1][-3:] == ["stop", "llama-server", "llama-moe"]


def test_guest_uses_pct_or_qm_by_type(env):
    runner = FakeRunner()
    assert call(env, "guest 211 shutdown", runner)[0] == 0
    assert runner.calls[-1] == ["/usr/sbin/pct", "shutdown", "211", "--timeout", "120"]
    assert call(env, "guest 212 start", runner)[0] == 0
    assert runner.calls[-1] == ["/usr/sbin/qm", "start", "212"]
    assert call(env, "guest 211 reboot", runner)[0] == 0
    assert runner.calls[-1] == ["/usr/sbin/pct", "reboot", "211", "--timeout", "120"]


def test_failed_command_answers_an_error(env):
    runner = FakeRunner({("/usr/sbin/qm", "start"): (2, "")})
    code, reply, logs, _ = call(env, "guest 212 start", runner)
    assert code == 1 and reply["ok"] is False and logs[-1][0] == syslog.LOG_ERR


def test_probe(env):
    gpus = "0, GPU, 41, 63.5, 27000, 40960\n1, GPU, 39, 60.1, 26500, 40960\n"
    runner = FakeRunner({("/usr/bin/nvidia-smi",): (0, gpus)})
    code, reply, _, _ = call(env, "probe", runner)
    assert code == 0 and len(reply["gpus"]) == 2 and reply["gpus"][1]["temperature.gpu"] == "39"


VM = ("/usr/sbin/qm", "guest", "exec", "212", "--timeout", "60", "--")


def test_client_checks_pause_schedules_their_own_resume(env):
    runner = FakeRunner({VM: (0, '{"exitcode": 0}')})
    code, reply, _, _ = call(env, "client-checks pause", runner)
    assert code == 0 and reply["auto_resume_minutes"] == 60
    pause = ["/usr/bin/systemctl", "--user", "--machine=agent@", "stop", "llm-check.timer"]
    resume = ["/usr/bin/systemctl", "--user", "--machine=agent@", "start", "llm-check.timer"]
    assert runner.calls == [
        [*VM, "/usr/bin/systemctl", "stop", "llm-ctl-resume-checks.timer"],
        [*VM, *pause],
        [
            *VM,
            "/usr/bin/systemd-run",
            "--on-active=60min",
            "--unit=llm-ctl-resume-checks",
            "--collect",
            *resume,
        ],
    ]
    call(env, "client-checks resume", runner)
    assert runner.calls[-2][-2:] == ["stop", "llm-ctl-resume-checks.timer"]
    assert runner.calls[-1] == [*VM, *resume]


def test_client_checks_failing_inside_the_vm(env):
    runner = FakeRunner({VM: (0, '{"exitcode": 1}')})
    code, reply, _, _ = call(env, "client-checks resume", runner)
    assert code == 1 and "exit 1" in reply["error"]


def test_never_a_shell(env):
    runner = FakeRunner({VM: (0, '{"exitcode": 0}')})
    for words in ("status", "llm switch moe", "guest 211 reboot", "probe", "client-checks pause"):
        call(env, words, runner)
    for argv in runner.calls:
        assert argv[0] not in ("sh", "bash", "/bin/sh", "/bin/bash") and "-c" not in argv


# The real runner


def test_runner_caps_output_and_replaces_bad_bytes():
    script = "import sys; sys.stdout.buffer.write(b'\\xff' + b'x' * 3_000_000)"
    result = llm_ctl.Runner().run([sys.executable, "-c", script], timeout=30)
    assert result.returncode == 0 and len(result.stdout) <= llm_ctl.OUTPUT_CAP
    assert result.stdout.startswith("\ufffd")


def test_runner_gives_a_child_no_stdin_and_a_fixed_environment(monkeypatch):
    monkeypatch.setenv("LLM_CTL_LEAK", "secret")
    script = "import os, sys; print(sorted(os.environ), sys.stdin.read() == '')"
    result = llm_ctl.Runner().run([sys.executable, "-c", script], timeout=30)
    assert "LLM_CTL_LEAK" not in result.stdout and result.stdout.strip().endswith("True")


def test_runner_kills_a_command_that_overruns():
    started = time.monotonic()
    with pytest.raises(llm_ctl.Failed, match="did not finish"):
        llm_ctl.Runner().run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    assert time.monotonic() - started < 10


def test_a_verb_stops_at_its_budget(env):
    agent = llm_ctl.Agent(llm_ctl.load_config(env["CONFIG"]), FakeRunner(), budget=0)
    with pytest.raises(llm_ctl.Failed, match="budget"):
        agent.do_llm("stop")


def test_the_example_config_loads(tmp_path):
    folder = tmp_path / "etc"
    folder.mkdir(mode=0o755)
    path = folder / "config.toml"
    path.write_text((PATH.parent / "config.example.toml").read_text())
    path.chmod(0o644)
    config = llm_ctl.load_config(str(path), os.getuid())
    assert config.checks["auto_resume"] == 60 and config.budget == 240
