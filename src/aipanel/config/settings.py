"""The settings (`settings.json`): what the operator tunes.

Owned by the app and changed only through the settings page, the JSON API or the CLI. No key
here also lives in the site config. Defaults come from the model, so an empty file is a working
panel. The field descriptions and limits are what the settings page shows.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from aipanel import weekly
from aipanel.config.site import HHMM, SiteConfig
from aipanel.weekly import Weekday


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScheduleRule(Model):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")
    enabled: bool = True
    days: list[Weekday] = Field(
        min_length=1, max_length=7, description="The days the off time falls on"
    )
    off: HHMM = Field(description="Power off at (site time)")
    on: HHMM = Field(description="Power on at; before the off time means the next day")

    @model_validator(mode="after")
    def _distinct(self) -> ScheduleRule:
        if self.off == self.on:
            raise ValueError("off and on must differ")
        if len(set(self.days)) != len(self.days):
            raise ValueError("a day is listed twice")
        return self


class ScheduleSettings(Model):
    enabled: bool = Field(False, description="Run the schedule rules")
    rules: list[ScheduleRule] = Field(default_factory=list, max_length=50)
    catch_up_minutes: int = Field(
        30, ge=1, le=720, description="Act on a boundary missed while the panel was down, up to"
    )
    busy_retry_minutes: int = Field(
        5, ge=1, le=60, description="When busy at an off time, try again every"
    )
    busy_grace_minutes: int = Field(
        30, ge=0, le=720, description="When busy at an off time, keep trying for"
    )
    force_after_grace: bool = Field(
        False, description="Still busy after that: shut down anyway (otherwise skip)"
    )
    boot_grace_minutes: int = Field(
        20, ge=0, le=240, description="Silences of a scheduled stop end this long after the on time"
    )

    @model_validator(mode="after")
    def _unique_names(self) -> ScheduleSettings:
        names = [r.name for r in self.rules]
        if len(set(names)) != len(names):
            raise ValueError("two rules have the same name")
        return self


class TimeoutSettings(Model):
    command_seconds: int = Field(
        300,
        ge=60,
        le=900,
        description="One llm-ctl command, at most: keep it above llm-ctl's limit_seconds (240)",
    )
    boot_seconds: int = Field(
        900, ge=60, le=3600, description="Power on or reboot until the host answers SSH"
    )
    llm_ready_seconds: int = Field(
        900, ge=30, le=3600, description="Until the LLM reports healthy after a start or switch"
    )
    shutdown_seconds: int = Field(
        600, ge=60, le=3600, description="A shutdown until the BMC reports power off"
    )
    guest_seconds: int = Field(300, ge=30, le=1800, description="A guest start or stop")


class CooldownSettings(Model):
    power_seconds: int = Field(60, ge=0, le=3600, description="Between two power actions")


class SilenceSettings(Model):
    shutdown_minutes: int = Field(
        720, ge=0, le=10080, description="A manual shutdown silences alerts for (default)"
    )
    reboot_minutes: int = Field(30, ge=0, le=720, description="A reboot silences alerts for")
    emergency_minutes: int = Field(
        120, ge=0, le=10080, description="An emergency action silences alerts for"
    )


class CostSettings(Model):
    standby_watts: float = Field(
        15, ge=0, le=2000, description="What the host draws while off (BMC on): measure it"
    )
    idle_watts: float | None = Field(
        None,
        ge=0,
        le=5000,
        description="What it draws while on and idle; empty: the 10th percentile of 7 days",
    )


class PollSettings(Model):
    interval_seconds: int = Field(15, ge=5, le=300, description="Read the host's state every")
    bmc_interval_seconds: int = Field(
        60, ge=10, le=3600, description="Ask the BMC every (more often while the host is down)"
    )


class DisplaySettings(Model):
    theme: Literal["auto", "light", "dark"] = Field("auto", description="Page colours")
    refresh_seconds: int = Field(5, ge=2, le=60, description="Refresh the dashboard every")
    activity_rows: int = Field(200, ge=10, le=2000, description="Rows on the activity page")


class Settings(Model):
    """The settings: app-owned, edited by the page, the API and the CLI."""

    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    timeouts: TimeoutSettings = Field(default_factory=TimeoutSettings)
    cooldowns: CooldownSettings = Field(default_factory=CooldownSettings)
    silences: SilenceSettings = Field(default_factory=SilenceSettings)
    cost: CostSettings = Field(default_factory=CostSettings)
    poll: PollSettings = Field(default_factory=PollSettings)
    display: DisplaySettings = Field(default_factory=DisplaySettings)

    @model_validator(mode="after")
    def _outside_protected_windows(self, info: ValidationInfo) -> Settings:
        """An off window may not overlap a protected window of the site config."""
        site = (info.context or {}).get("site")
        if not isinstance(site, SiteConfig):
            return self
        for rule in self.schedule.rules:
            off = weekly.spans(rule.days, rule.off, rule.on)
            for window in site.protected_windows:
                if weekly.overlaps(off, weekly.spans(window.days, window.start, window.end)):
                    raise ValueError(
                        f"rule {rule.name!r} overlaps the protected window {window.reason!r} "
                        f"({','.join(window.days)} {window.start}-{window.end})"
                    )
        return self


def validate(data: object, site: SiteConfig | None) -> Settings:
    return Settings.model_validate(data, context={"site": site})
