"""Weekly time windows ("mon-fri 23:00 to 07:00"), as minute spans of a week.

Schedule rules, protected windows and static tariffs all use the same shape: a set of days
and a start and end time, where an end at or before the start means the window crosses
midnight into the next day. A window is turned into spans of minutes since Monday 00:00,
wrapped at the end of the week, so overlap and membership are plain interval checks.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Literal

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEK = 7 * 24 * 60
DAY = 24 * 60

Span = tuple[int, int]


def minutes(hhmm: str) -> int:
    hours, mins = hhmm.split(":")
    return int(hours) * 60 + int(mins)


def spans(days: Iterable[Weekday], start: str, end: str) -> list[Span]:
    """The window's spans in minutes of the week, each within [0, WEEK)."""
    begin, finish = minutes(start), minutes(end)
    length = finish - begin if finish > begin else finish + DAY - begin
    out: list[Span] = []
    for day in days:
        first = WEEKDAYS.index(day) * DAY + begin
        last = first + length
        if last <= WEEK:
            out.append((first, last))
        else:  # Sunday night into Monday morning
            out.append((first, WEEK))
            out.append((0, last - WEEK))
    return out


def overlaps(a: Sequence[Span], b: Sequence[Span]) -> bool:
    return any(s1 < e2 and s2 < e1 for s1, e1 in a for s2, e2 in b)


def minute_of_week(local: datetime) -> int:
    return local.weekday() * DAY + local.hour * 60 + local.minute


def contains(window: Sequence[Span], local: datetime) -> bool:
    minute = minute_of_week(local)
    return any(start <= minute < end for start, end in window)
