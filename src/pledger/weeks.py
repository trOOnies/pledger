"""weeks.py: the weeks of pledger, from Monday to Sunday, and the months they belong to."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Week:
    """
    A week, from its Monday to its Sunday. It belongs to the month that has at least 4
    of its days: the one of its Thursday, so each week belongs to a single month, and
    each month has 4 or 5 weeks.
    """

    start: dt.date  # its Monday

    def __post_init__(self):
        if self.start.weekday() != 0:
            raise ValueError(f"A week starts on a Monday, not on {self.start:%A} {self.start}")

    @classmethod
    def of(cls, day: dt.date) -> Week:
        """The week of a day."""
        return cls(day - dt.timedelta(days=day.weekday()))

    @classmethod
    def of_month(cls, month: str) -> list[Week]:
        """The weeks of a YYYY-MM month, in order."""
        first = dt.date.fromisoformat(f"{month}-01")
        thursday = first + dt.timedelta(days=(3 - first.weekday()) % 7)  # the first one of the month
        weeks = []
        while thursday.month == first.month:
            weeks.append(cls(thursday - dt.timedelta(days=3)))
            thursday += dt.timedelta(weeks=1)
        return weeks

    @property
    def end(self) -> dt.date:
        """Its Sunday."""
        return self.start + dt.timedelta(days=6)

    @property
    def month(self) -> str:
        """The YYYY-MM month it belongs to."""
        return (self.start + dt.timedelta(days=3)).strftime("%Y-%m")


def prorate(total: float, parts: int) -> list[float]:
    """
    A total split into equal parts, to the cent, adding up to it exactly: the cents
    left over go one each to the first parts.
    """
    if parts <= 0:
        raise ValueError(f"Cannot split into {parts} parts")
    cents = round(total * 100)
    each, left = divmod(cents, parts)
    return [(each + (i < left)) / 100 for i in range(parts)]


def prorate_by_week(month: str, total: float) -> list[tuple[Week, float]]:
    """A total of a YYYY-MM month split equally between its weeks (see prorate)."""
    weeks = Week.of_month(month)
    return list(zip(weeks, prorate(total, len(weeks))))
