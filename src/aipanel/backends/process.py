"""Running a command: an argument list, never a shell, always with a timeout."""

import asyncio
import contextlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str


class CommandTimeout(Exception):
    pass


class Run(Protocol):
    async def __call__(
        self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None
    ) -> Result: ...


async def run(
    argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None
) -> Result:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env) if env is not None else None,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        raise CommandTimeout(f"{argv[0]} did not finish within {timeout:g} s") from None
    finally:
        # Also when the caller is cancelled: never leave an ssh or ipmitool child behind.
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await asyncio.shield(proc.wait())
    return Result(
        proc.returncode if proc.returncode is not None else -1,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )
