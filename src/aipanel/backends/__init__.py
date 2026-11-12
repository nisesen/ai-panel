"""What the panel talks to. `Backends` is one set of them, built from a site config."""

from dataclasses import dataclass

from aipanel.backends.protocols import (
    HostAgent,
    LlmClient,
    Metrics,
    PowerBackend,
    PowerSource,
    PriceSource,
    Silences,
)


@dataclass(frozen=True)
class Backends:
    power: PowerBackend
    agent: HostAgent
    llm: LlmClient
    metrics: Metrics | None = None
    silences: Silences | None = None
    power_source: PowerSource | None = None
    price_source: PriceSource | None = None
