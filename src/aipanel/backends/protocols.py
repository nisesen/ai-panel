"""The interfaces the panel talks to, and the plain data they return.

Each has one real implementation (power.py, agent.py, llm.py, metrics.py, silences.py,
energy.py) and a fake in demo.py. Another kind of BMC or host is one new class with the same
methods, chosen in panel.py; nothing else changes.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

PowerState = Literal["on", "off", "unknown"]


class BackendError(Exception):
    """A backend answered, with an error."""


class Unreachable(BackendError):
    """A backend could not be reached at all (no answer, connection refused, timeout)."""


@dataclass(frozen=True)
class Sensor:
    name: str
    value: str
    status: str


@dataclass(frozen=True)
class Sample:
    labels: dict[str, str]
    value: float


@dataclass(frozen=True)
class Series:
    labels: dict[str, str]
    points: list[tuple[float, float]] = field(default_factory=list)  # (unix time, value)


@dataclass(frozen=True)
class Silence:
    id: str
    ends_at: datetime
    comment: str


@dataclass(frozen=True)
class PricePoint:
    price: float
    period: str | None = None


class PowerBackend(Protocol):
    """The machine's power, out of band (a BMC)."""

    async def status(self) -> PowerState: ...
    async def on(self) -> None: ...
    async def soft(self) -> None: ...
    async def off(self) -> None: ...
    async def reset(self) -> None: ...
    async def sensors(self) -> list[Sensor]: ...
    async def events(self, limit: int = 10) -> list[str]: ...


class HostAgent(Protocol):
    """The host agent (llm-ctl): one verb per call, a JSON object back."""

    async def call(self, args: Sequence[str], timeout: float) -> dict[str, Any]: ...


class LlmClient(Protocol):
    """The LLM server (llama.cpp's HTTP endpoints)."""

    async def health(self) -> bool: ...
    async def props(self) -> dict[str, Any]: ...
    async def slots(self) -> tuple[int, int] | None:
        """(busy, total), or None when the server does not expose its slots."""
        ...


class Metrics(Protocol):
    async def query(self, promql: str) -> list[Sample]: ...
    async def query_range(
        self, promql: str, start: datetime, end: datetime, step: int
    ) -> list[Series]: ...


class Silences(Protocol):
    """Alert silences for a planned stop, using the matchers of the site config."""

    async def create(self, until: datetime, comment: str) -> str: ...
    async def active(self) -> list[Silence]:
        """The panel's own silences that are in force."""
        ...

    async def expire(self, silence_id: str) -> None: ...


Grid = dict[int, float | None]


class PowerSource(Protocol):
    async def watts(self) -> float | None:
        """Watts now, or None when the host is off or nothing is known."""
        ...

    async def history(self, start: datetime, end: datetime, step: int) -> Grid:
        """Watts per step (unix time, aligned to the step): a number while on, None while off.

        A step with no key is unknown, and counts neither way.
        """
        ...


class PriceSource(Protocol):
    currency: str
    estimated_future: bool  # True when future prices are guessed from the past

    async def now(self) -> PricePoint: ...
    async def history(self, start: datetime, end: datetime, step: int) -> dict[int, PricePoint]: ...
    async def future(self, start: datetime, end: datetime, step: int) -> dict[int, PricePoint]: ...
