"""llm-ctl: the verbs it accepts, the ones it refuses, and the exact commands it runs.

Loaded from its file (it has no .py extension), and driven with a fake command runner.
"""

import importlib.machinery
import importlib.util
import io
import json
import syslog
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
pause = ["systemctl", "--user", "--machine=agent@", "stop", "llm-check.timer"]
resume = ["systemctl", "--user", "--machine=agent@", "start", "llm-check.timer"]
"""

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
            ("/usr/sbin/pct", "exec", "210", "--", "systemctl", "is-active"): (
                3,
                "active\ninactive\n",
            ),
            ("/usr/sbin/pct", "exec", "210", "--", "curl", "-s", "-o"): (0, "200"),
            ("/usr/sbin/pct", "exec", "210", "--", "cat"): (0, SECRET_KEY + "\n"),
            ("/usr/sbin/pct", "exec", "210", "--", "curl", "-s", "-m"): (
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
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    return {"LLM_CTL_CONFIG": str(path), "SSH_CLIENT": "192.0.2.50 40000 22"}


def call(env, words, runner=None, euid=0, argv=None):
    out, logs, execs = io.StringIO(), [], []
    environ = dict(env)
    if words is not None:
        environ["SSH_ORIGINAL_COMMAND"] = words
    code = llm_ctl.main(
        argv=argv or [],
        environ=environ,
        runner=runner or FakeRunner(),
        out=out,
        log=lambda prio, msg: logs.append((prio, msg)),
        euid=euid,
        execv=lambda path, args: execs.append(args),
        read_file=lambda p: {
            "/proc/uptime": "3600.5 100",
            "/proc/sys/kernel/random/boot_id": "b1\n",
        }[p],
    )
    text = out.getvalue().strip()
    return code, json.loads(text) if text else None, logs, execs


@pytest.fixture
def config(env):
    return llm_ctl.load_config(env["LLM_CTL_CONFIG"])


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


def test_bad_config_answers_an_error(env, tmp_path):
    (tmp_path / "config.toml").write_text("[llm]\n")
    code, reply, _, _ = call(env, "status")
    assert code == 1 and "llm.ct" in reply["error"]


def test_unknown_command_override_is_a_config_error(tmp_path, env):
    Path(env["LLM_CTL_CONFIG"]).write_text(CONFIG + '\n[commands]\nbash = ["/bin/bash"]\n')
    code, reply, _, _ = call(env, "status")
    assert code == 1 and "commands.bash" in reply["error"]


# Unprivileged: check, then sudo itself with the checked words


def test_unprivileged_reexecs_through_sudo(env):
    _code, _reply, logs, execs = call(env, "llm switch moe", euid=1001)
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
    assert reply["llm"]["health"] == 200 and reply["llm"]["model_alias"] == "dense-32b"
    assert logs[-1][0] == syslog.LOG_DEBUG


def test_status_reads_props_with_the_key_on_stdin_only(env):
    runner = FakeRunner()
    call(env, "status", runner)
    assert all(SECRET_KEY not in " ".join(c) for c in runner.calls)
    assert f"Authorization: Bearer {SECRET_KEY}\n" in runner.inputs
    props = runner.ran("/usr/sbin/pct", "exec", "210", "--", "curl", "-s", "-m")[0]
    assert props[-3:] == ["-H", "@-", "http://127.0.0.1:8080/props"]


def test_shutdown_stops_the_llm_then_answers_then_powers_off(env):
    runner = FakeRunner()
    out = io.StringIO()
    seen_at_poweroff = []
    runner.on_call = lambda argv: (
        seen_at_poweroff.append(out.getvalue()) if "poweroff" in argv else None
    )
    code = llm_ctl.main(
        argv=[],
        environ={**env, "SSH_ORIGINAL_COMMAND": "shutdown"},
        runner=runner,
        out=out,
        log=lambda *a: None,
        euid=0,
        execv=None,
    )
    assert code == 0
    assert runner.calls[0] == ["/usr/sbin/pct", "shutdown", "210", "--timeout", "120"]
    assert runner.calls[-1] == ["/usr/bin/systemctl", "poweroff"]
    assert '"ok":true' in seen_at_poweroff[0]  # the answer was out before the power went


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
    code, _reply, _, _ = call(env, "reboot", runner)
    assert code == 0 and runner.calls[-1] == ["/usr/bin/systemctl", "reboot"]


def test_llm_switch(env):
    runner = FakeRunner()
    code, reply, _, _ = call(env, "llm switch moe", runner)
    assert code == 0 and reply["unit"] == "llama-moe"
    ct = ["/usr/sbin/pct", "exec", "210", "--", "systemctl"]
    assert runner.calls == [
        [*ct, "stop", "llama-server"],
        [*ct, "reset-failed", "llama-server", "llama-moe"],
        [*ct, "start", "llama-moe"],
    ]


def test_llm_restart_restarts_the_active_unit(env):
    runner = FakeRunner()
    code, reply, _, _ = call(env, "llm restart", runner)
    assert code == 0 and reply["model"] == "dense"
    assert runner.calls[-1][-2:] == ["restart", "llama-server"]


def test_llm_restart_fails_without_an_active_unit(env):
    runner = FakeRunner(
        {
            ("/usr/sbin/pct", "exec", "210", "--", "systemctl", "is-active"): (
                3,
                "inactive\ninactive\n",
            )
        }
    )
    code, reply, _, _ = call(env, "llm restart", runner)
    assert code == 1 and "no model" in reply["error"]


def test_llm_start_starts_the_default_when_none_runs(env):
    runner = FakeRunner(
        {
            ("/usr/sbin/pct", "exec", "210", "--", "systemctl", "is-active"): (
                3,
                "inactive\nfailed\n",
            )
        }
    )
    code, _reply, _, _ = call(env, "llm start", runner)
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


def test_failed_command_answers_an_error(env):
    runner = FakeRunner({("/usr/sbin/qm", "start"): (2, "")})
    code, reply, logs, _ = call(env, "guest 212 start", runner)
    assert code == 1 and reply["ok"] is False and logs[-1][0] == syslog.LOG_ERR


def test_probe(env):
    gpus = "0, GPU, 41, 63.5, 27000, 40960\n1, GPU, 39, 60.1, 26500, 40960\n"
    runner = FakeRunner({("/usr/bin/nvidia-smi",): (0, gpus)})
    code, reply, _, _ = call(env, "probe", runner)
    assert code == 0 and len(reply["gpus"]) == 2 and reply["gpus"][1]["temperature.gpu"] == "39"


def test_client_checks(env):
    runner = FakeRunner({("/usr/sbin/qm", "guest", "exec"): (0, '{"exitcode": 0}')})
    code, _reply, _, _ = call(env, "client-checks pause", runner)
    assert code == 0
    assert runner.calls[-1] == [
        "/usr/sbin/qm",
        "guest",
        "exec",
        "212",
        "--timeout",
        "60",
        "--",
        "systemctl",
        "--user",
        "--machine=agent@",
        "stop",
        "llm-check.timer",
    ]


def test_client_checks_failing_inside_the_vm(env):
    runner = FakeRunner({("/usr/sbin/qm", "guest", "exec"): (0, '{"exitcode": 1}')})
    code, reply, _, _ = call(env, "client-checks resume", runner)
    assert code == 1 and "exit 1" in reply["error"]


def test_never_a_shell(env):
    runner = FakeRunner({("/usr/sbin/qm", "guest", "exec"): (0, '{"exitcode": 0}')})
    for words in ("status", "llm switch moe", "guest 211 reboot", "probe", "client-checks pause"):
        call(env, words, runner)
    for argv in runner.calls:
        assert argv[0] not in ("sh", "bash", "/bin/sh", "/bin/bash") and "-c" not in argv
