"""The site config (`config.yaml`): where the panel connects and what it may do.

Owned by git and read-only to the app. Nothing here can be changed from the page, the API or
the CLI, so a stolen browser session can never point the panel at another target. Secrets are
never in it: it names an environment variable or a file for each.
"""

import ipaddress
import os
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from aipanel.weekly import Weekday

HHMM = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
Token = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")]

ActionName = Literal[
    "power_on",
    "shutdown",
    "reboot",
    "emergency_soft_off",
    "emergency_off",
    "emergency_reset",
    "llm_switch",
    "llm_restart",
    "llm_stop",
    "llm_start",
    "guest_start",
    "guest_shutdown",
    "guest_reboot",
    "probe",
    "client_checks",
]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class SecretRef(Model):
    """A secret's source: exactly one of an environment variable or a file."""

    env: str | None = Field(None, description="Name of an environment variable holding it")
    file: Path | None = Field(
        None, description="Path of a file holding it (trailing newline ignored)"
    )

    @model_validator(mode="after")
    def _one_source(self) -> SecretRef:
        if (self.env is None) == (self.file is None):
            raise ValueError("give exactly one of env or file")
        return self

    def read(self) -> str:
        if self.env is not None:
            value = os.environ.get(self.env)
            if value is None:
                raise LookupError(f"environment variable {self.env} is not set")
            return value
        assert self.file is not None
        return self.file.read_text().strip()


class Link(Model):
    label: str
    url: str


class SiteInfo(Model):
    name: Token = Field(
        description="The GPU host's name. Typed to confirm power and emergency actions."
    )
    timezone: str = Field("UTC", description="IANA time zone for schedules, tariffs and the page")
    links: list[Link] = Field(default_factory=list, description="Links shown in the header")
    emergency_warning: str = Field(
        "This cuts power like pulling the plug: everything on the host dies without shutting "
        "down. Use it only for a host that no longer answers, or a shutdown that is stuck.",
        description="Shown above the emergency actions; describe your hardware's risks here",
    )

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError, ValueError:
            raise ValueError(f"unknown time zone {value!r}") from None
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class AuthConfig(Model):
    mode: Literal["trusted_header", "none"] = Field(
        description="trusted_header: a proxy you control signs people in and sets the headers. "
        "none: no sign-in at all, accepted only in DEMO mode."
    )
    user_header: str = "Remote-User"
    groups_header: str = "Remote-Groups"
    groups_separator: str = Field(
        ",",
        min_length=1,
        description="Between groups in the groups header (Authentik uses '|'); a group whose "
        "name contains it cannot be matched",
    )
    allowed_groups: list[str] = Field(
        default_factory=list, description="A request needs one of these groups"
    )
    trusted_proxies: list[str] = Field(
        default_factory=list,
        description="Addresses or CIDRs of the proxy that sets the headers; others are refused. "
        "Required with trusted_header; ['0.0.0.0/0', '::/0'] accepts anyone (dangerous)",
    )

    @field_validator("trusted_proxies")
    @classmethod
    def _networks(cls, value: list[str]) -> list[str]:
        for network in value:
            try:
                ipaddress.ip_network(network, strict=False)
            except ValueError:
                raise ValueError(f"not an address or CIDR: {network!r}") from None
        return value

    @model_validator(mode="after")
    def _headers_need_a_proxy(self) -> AuthConfig:
        if self.mode == "trusted_header" and not self.allowed_groups:
            raise ValueError("trusted_header needs at least one entry in allowed_groups")
        if self.mode == "trusted_header" and not self.trusted_proxies:
            raise ValueError(
                "trusted_header needs auth.trusted_proxies: the addresses of the proxy that sets "
                "the headers (['0.0.0.0/0', '::/0'] accepts anyone, which is dangerous)"
            )
        return self


class BmcConfig(Model):
    kind: Literal["ipmi"] = "ipmi"
    host: str = Field(description="The BMC's address")
    port: int = Field(623, ge=1, le=65535)
    username: SecretRef
    password: SecretRef
    interface: Literal["lanplus", "lan"] = "lanplus"
    cipher_suite: int = Field(3, ge=0, le=17, description="ipmitool -C")
    privilege: Literal["OPERATOR", "ADMINISTRATOR", "USER"] = Field(
        "OPERATOR", description="ipmitool -L; an Operator account can switch power, nothing else"
    )
    timeout_seconds: float = Field(20, gt=0, le=120)
    command: str = Field("ipmitool", description="The ipmitool binary")


class Guest(Model):
    id: int = Field(ge=100, le=999_999_999)
    name: str


class HostConfig(Model):
    address: str = Field(description="The GPU host, reached over SSH")
    port: int = Field(22, ge=1, le=65535)
    user: Token = "llmctl"
    key_file: Path = Field(description="The panel's private SSH key")
    known_hosts_file: Path = Field(description="A known_hosts file with the host's key")
    connect_timeout_seconds: int = Field(10, ge=1, le=60)
    guests: list[Guest] = Field(
        default_factory=list, description="Guests the panel shows and may start or stop"
    )
    llm_guest: int | None = Field(None, description="The guest (container) that runs llama.cpp")
    command: str = Field("ssh", description="The ssh binary")

    @model_validator(mode="after")
    def _llm_guest_listed(self) -> HostConfig:
        if self.llm_guest is not None and self.llm_guest not in {g.id for g in self.guests}:
            raise ValueError("llm_guest must be one of guests")
        return self


class LlmModel(Model):
    name: Token = Field(description="The model's name in llm-ctl's config")
    label: str
    match: str | None = Field(
        None, description="Text in /props model_alias or model_path that identifies it"
    )


class LlmConfig(Model):
    url: str = Field(description="llama.cpp's server, for /health, /props and /slots")
    api_key: SecretRef | None = None
    models: list[LlmModel] = Field(default_factory=list)
    timeout_seconds: float = Field(5, gt=0, le=60)
    pause_client_checks: bool = Field(
        False,
        description="Pause and resume a client's checks of the LLM (user timers in a guest VM, "
        "llm-ctl client-checks) around planned LLM stops, so they do not page anyone",
    )


class GpuQueries(Model):
    label: str = Field("gpu", description="The label that tells the GPUs apart")
    temperature: str | None = None
    power: str | None = None
    memory_used: str | None = Field(None, description="Bytes")
    memory_total: str | None = Field(None, description="Bytes")


class PrometheusConfig(Model):
    url: str
    timeout_seconds: float = Field(10, gt=0, le=60)
    gpus: GpuQueries | None = None
    decode_tokens_per_second: str | None = Field(None, description="A query for decode speed")
    retention_days: int = Field(
        15, ge=1, le=3650, description="How far back Prometheus keeps data (cost history)"
    )


class Matcher(Model):
    name: str
    value: str
    is_regex: bool = False
    is_equal: bool = True


class SilenceSet(Model):
    matchers: list[Matcher] = Field(
        min_length=1, description="All of them must match: Alertmanager ANDs a silence's matchers"
    )


class AlertmanagerConfig(Model):
    url: str
    silences: list[SilenceSet] = Field(
        min_length=1,
        description="What a planned stop silences: one silence per set, so the sets add up (OR) "
        "while the matchers inside a set must all match (AND)",
    )
    created_by: str = "ai-panel"
    timeout_seconds: float = Field(10, gt=0, le=60)


class ActionsConfig(Model):
    enabled: list[ActionName] | None = Field(
        None, description="The actions that exist on this panel; all when not given"
    )
    dry_run: bool = Field(
        False, description="Read everything for real, but only log what would be changed"
    )


class TariffWindow(Model):
    days: list[Weekday] = Field(min_length=1, max_length=7)
    start: HHMM = Field(alias="from")
    end: HHMM = Field(alias="to")
    period: str
    months: list[Annotated[int, Field(ge=1, le=12)]] | None = Field(
        None, description="Months the window applies in (seasons); all when not given"
    )


class Tariff(Model):
    currency: str = "USD"
    periods: dict[str, Annotated[float, Field(ge=0)]] = Field(
        min_length=1, description="Price per kWh of each period"
    )
    default: str = Field(description="The period outside every window")
    windows: list[TariffWindow] = Field(
        default_factory=list, description="The first window that matches wins"
    )

    @model_validator(mode="after")
    def _periods_exist(self) -> Tariff:
        names = {self.default, *(w.period for w in self.windows)}
        missing = sorted(names - set(self.periods))
        if missing:
            raise ValueError(f"periods without a price: {', '.join(missing)}")
        return self


class PrometheusPower(Model):
    source: Literal["prometheus"]
    query: str = Field(description="The host's power in watts; no data means it is off")


class StaticPower(Model):
    source: Literal["static"]
    watts: float = Field(gt=0, description="The host's power while on")


class PrometheusPrice(Model):
    source: Literal["prometheus"]
    query: str = Field(description="The price per kWh now")
    period_label: str | None = Field("period", description="Label naming the tariff period")
    currency: str = "USD"


class StaticPrice(Model):
    source: Literal["static"]
    tariff: Tariff


class CostConfig(Model):
    power: Annotated[PrometheusPower | StaticPower, Field(discriminator="source")]
    price: Annotated[PrometheusPrice | StaticPrice, Field(discriminator="source")]


class ProtectedWindow(Model):
    days: list[Weekday] = Field(min_length=1, max_length=7)
    start: HHMM = Field(alias="from")
    end: HHMM = Field(alias="to")
    reason: str


class SiteConfig(Model):
    """The site config: git-owned, read-only to the app."""

    site: SiteInfo
    auth: AuthConfig
    bmc: BmcConfig
    host: HostConfig
    llm: LlmConfig
    prometheus: PrometheusConfig | None = None
    alertmanager: AlertmanagerConfig | None = None
    actions: ActionsConfig = Field(default_factory=ActionsConfig)
    cost: CostConfig | None = None
    protected_windows: list[ProtectedWindow] = Field(
        default_factory=list, description="No scheduled or manual stop inside these (backups)"
    )

    @model_validator(mode="after")
    def _prometheus_for_queries(self) -> SiteConfig:
        uses = self.cost is not None and "prometheus" in (
            self.cost.power.source,
            self.cost.price.source,
        )
        if uses and self.prometheus is None:
            raise ValueError("cost reads Prometheus, but there is no prometheus section")
        return self

    def action_enabled(self, name: str) -> bool:
        return self.actions.enabled is None or name in self.actions.enabled

    def model(self, name: str) -> LlmModel | None:
        return next((m for m in self.llm.models if m.name == name), None)

    def guest(self, guest_id: int) -> Guest | None:
        return next((g for g in self.host.guests if g.id == guest_id), None)
