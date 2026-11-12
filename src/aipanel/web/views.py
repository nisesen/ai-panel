"""Templates, their filters, and the shapes the pages draw (the week's timeline)."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.templating import Jinja2Templates

from aipanel import weekly
from aipanel.actions import registry
from aipanel.actions.base import Action
from aipanel.config.site import SiteConfig
from aipanel.scheduler import Boundary

TEMPLATES = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=TEMPLATES)

CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CAD": "$", "AUD": "$"}


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    mins = rest // 60
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {mins} min"
    return f"{mins} min" if mins else f"{seconds} s"


def money(value: float | None, currency: str = "", digits: int = 2) -> str:
    if value is None:
        return "–"
    symbol = CURRENCY_SYMBOLS.get(currency)
    return f"{symbol}{value:,.{digits}f}" if symbol else f"{value:,.{digits}f} {currency}".strip()


def number(value: float | None, digits: int = 0, unit: str = "") -> str:
    if value is None:
        return "–"
    text = f"{value:,.{digits}f}"
    return f"{text} {unit}" if unit else text


def gib(value: float | None) -> str:
    return "–" if value is None else f"{value / 1024**3:.1f}"


templates.env.filters.update(duration=duration, money=money, number=number, gib=gib)


def local(site: SiteConfig, when: datetime | None, fmt: str = "%a %d %b %H:%M") -> str:
    return "–" if when is None else when.astimezone(site.site.tz).strftime(fmt)


def action_groups(site: SiteConfig) -> dict[str, list[Action]]:
    groups: dict[str, list[Action]] = {}
    for action in registry.enabled(site):
        groups.setdefault(action.group, []).append(action)
    return groups


@dataclass(frozen=True)
class Bar:
    left: float  # percent of the day
    width: float
    title: str


@dataclass(frozen=True)
class Day:
    label: str
    off: tuple[Bar, ...]
    protected: tuple[Bar, ...]


def _bar(start: datetime, end: datetime, day: datetime, title: str) -> Bar | None:
    lo, hi = max(start, day), min(end, day + timedelta(days=1))
    if hi <= lo:
        return None
    total = 86400
    left = (lo - day).total_seconds() / total * 100
    return Bar(round(left, 2), round((hi - lo).total_seconds() / total * 100, 2), title)


def timeline(
    site: SiteConfig, upcoming: list[tuple[Boundary, str]], now: datetime, days: int = 7
) -> list[Day]:
    """The next days, each with its off windows and protected windows as bars on 24 hours."""
    tz = site.site.tz
    first = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    windows: list[tuple[datetime, datetime, str]] = []
    off_at: dict[str, datetime] = {}
    for b, note in upcoming:
        if b.kind == "off":
            off_at[b.rule] = b.at
        else:
            start = off_at.pop(b.rule, now)  # an off window already running started earlier
            title = f"{b.rule}: off {local(site, start, '%H:%M')} – on {local(site, b.at, '%H:%M')}"
            windows.append((start, b.at, title + (f" ({note})" if note else "")))
    for rule, start in off_at.items():
        windows.append(
            (start, first + timedelta(days=days), f"{rule}: off from {local(site, start)}")
        )
    out = []
    for i in range(days):
        day = first + timedelta(days=i)
        off = tuple(bar for s, e, t in windows if (bar := _bar(s, e, day, t)) is not None)
        monday = day - timedelta(days=day.weekday())
        protected = []
        for w in site.protected_windows:
            for lo, hi in weekly.spans(w.days, w.start, w.end):
                begin, end = monday + timedelta(minutes=lo), monday + timedelta(minutes=hi)
                bar = _bar(begin, end, day, f"protected: {w.reason} {w.start}-{w.end}")
                if bar is not None:
                    protected.append(bar)
        out.append(Day(day.strftime("%a %d %b"), off, tuple(protected)))
    return out
