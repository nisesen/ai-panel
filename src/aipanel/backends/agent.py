"""The host agent llm-ctl, over SSH.

The panel's key may run only llm-ctl on the host (a forced command), which reads the verb from
SSH_ORIGINAL_COMMAND and answers one JSON object. The verb's words come from fixed lists in the
actions, never from free text; they are checked here once more before they leave.
"""

import json
import os
import re
from collections.abc import Sequence
from typing import Any

from aipanel.backends.process import CommandTimeout, Run, run
from aipanel.backends.protocols import BackendError, Timeout, Unreachable
from aipanel.config.site import HostConfig

WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SshAgent:
    def __init__(self, config: HostConfig, runner: Run = run) -> None:
        self.config = config
        self.run = runner

    def argv(self, args: Sequence[str]) -> list[str]:
        c = self.config
        if not args or not all(WORD.match(a) for a in args):
            raise BackendError(f"refusing to send {' '.join(args)!r}")
        return [
            c.command,
            "-F",
            "none",  # no ssh_config: every option is given here
            "-i",
            str(c.key_file),
            "-p",
            str(c.port),
            "-l",
            c.user,
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={c.known_hosts_file}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            f"ConnectTimeout={c.connect_timeout_seconds}",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "LogLevel=ERROR",
            "-T",
            c.address,
            *args,
        ]

    async def call(self, args: Sequence[str], timeout: float) -> dict[str, Any]:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp"}  # noqa: S108 - ssh needs a HOME; it writes nothing there
        try:
            result = await self.run(self.argv(args), timeout=timeout, env=env)
        except CommandTimeout as e:
            raise Timeout(f"host agent: {e}") from None
        except OSError as e:
            raise BackendError(f"host agent: cannot run {self.config.command}: {e}") from None
        if result.returncode == 255:  # ssh's own failure: no connection, refused key, ...
            detail = result.stderr.strip().splitlines()
            raise Unreachable(f"host agent: {detail[-1] if detail else 'ssh failed'}")
        return parse_reply(result.stdout, result.returncode, result.stderr)


def parse_reply(stdout: str, returncode: int, stderr: str = "") -> dict[str, Any]:
    try:
        reply = json.loads(stdout)
    except json.JSONDecodeError:
        detail = (stderr or stdout).strip()[-300:]
        raise BackendError(f"host agent: not a JSON answer (exit {returncode}): {detail}") from None
    if not isinstance(reply, dict):
        raise BackendError("host agent: the answer is not a JSON object")
    if returncode != 0 or reply.get("ok") is False:
        raise BackendError(f"host agent: {reply.get('error') or f'exit {returncode}'}")
    return reply
