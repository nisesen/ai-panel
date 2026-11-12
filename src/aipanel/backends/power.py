"""Power through the BMC, with ipmitool over the network (IPMI v2 'lanplus').

The password never appears on the command line: ipmitool reads it from a file (-f) or from
its environment (-E).
"""

import os
from collections.abc import Sequence

from aipanel.backends.process import CommandTimeout, Run, run
from aipanel.backends.protocols import BackendError, PowerState, Sensor, Timeout, Unreachable
from aipanel.config.site import BmcConfig


class IpmiPower:
    def __init__(self, config: BmcConfig, runner: Run = run) -> None:
        self.config = config
        self.run = runner

    def _base(self) -> tuple[list[str], dict[str, str]]:
        c = self.config
        argv = [c.command, "-I", c.interface, "-H", c.host, "-p", str(c.port)]
        argv += ["-U", c.username.read(), "-L", c.privilege, "-C", str(c.cipher_suite)]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        if c.password.file is not None:
            argv += ["-f", str(c.password.file)]
        else:
            argv.append("-E")
            env["IPMI_PASSWORD"] = c.password.read()
        return argv, env

    async def _ipmi(self, *args: str) -> str:
        try:
            argv, env = self._base()
        except (OSError, LookupError) as e:
            raise BackendError(f"BMC credentials: {e}") from None
        try:
            result = await self.run([*argv, *args], timeout=self.config.timeout_seconds, env=env)
        except CommandTimeout as e:
            raise Timeout(f"BMC: {e}") from None
        except OSError as e:
            raise BackendError(f"BMC: cannot run {self.config.command}: {e}") from None
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip().splitlines()
            detail = message[-1] if message else f"exit {result.returncode}"
            lowered = detail.lower()
            if "unable to establish" in lowered or "no response" in lowered:
                raise Unreachable(f"BMC: {detail}")
            raise BackendError(f"BMC: {detail}")
        return result.stdout

    async def status(self) -> PowerState:
        out = (await self._ipmi("chassis", "power", "status")).lower()
        if out.rstrip().endswith(" on"):
            return "on"
        if out.rstrip().endswith(" off"):
            return "off"
        return "unknown"

    async def on(self) -> None:
        await self._ipmi("chassis", "power", "on")

    async def soft(self) -> None:
        await self._ipmi("chassis", "power", "soft")

    async def off(self) -> None:
        await self._ipmi("chassis", "power", "off")

    async def reset(self) -> None:
        await self._ipmi("chassis", "power", "reset")

    async def sensors(self) -> list[Sensor]:
        return parse_sdr(await self._ipmi("sdr", "list"))

    async def events(self, limit: int = 10) -> list[str]:
        out = await self._ipmi("sel", "elist", "last", str(limit))
        return [line.strip() for line in out.splitlines() if line.strip()]


def parse_sdr(text: str) -> list[Sensor]:
    """`ipmitool sdr list` rows: 'CPU Temp | 45 degrees C | ok'; unread sensors are left out."""
    sensors = []
    for line in text.splitlines():
        parts: Sequence[str] = [p.strip() for p in line.split("|")]
        if len(parts) == 3 and parts[1] and "no reading" not in parts[1].lower():
            sensors.append(Sensor(parts[0], parts[1], parts[2]))
    return sensors
