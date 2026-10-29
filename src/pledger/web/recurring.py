"""recurring.py: the movements that repeat every month or week, suggested when due."""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import yaml

from pledger.enums import LedgerSide
from pledger.web import store
from pledger.web.store import Snapshot, _parse_amount

# The recurring movements are never written by pledger: each one is suggested in the Ledger tab
# from the date it is due on, until a movement of its base movement and accounts is in the
# ledger CSV in its period (its month, or its week from Monday to Sunday), and up to UPCOMING
# days before it is due.
UPCOMING = 7
WEEKDAYS = [d.lower() for d in calendar.day_name]  # monday, tuesday...


@dataclass(frozen=True)
class Recurring:
    """A movement of the recurring YAML, due on a `day` of each month or a `weekday` of each week."""

    name: str
    base_movement: str
    acc_debit: str
    acc_credit: str
    amount: float | None  # None: filled in when adding it
    comment: str
    day: int | None  # 1-31: monthly, on the last day of the shorter months
    weekday: int | None  # 0 (Monday) - 6: weekly
    start: str  # YYYY-MM: the first month
    end: str | None  # YYYY-MM: the last month, if any

    def dates(self, until: dt.date) -> list[dt.date]:
        """The dates it is due on, from its start up to `until` (and its end)."""
        first = dt.date.fromisoformat(f"{self.start}-01")
        if self.end:
            year, month = map(int, self.end.split("-"))
            until = min(until, dt.date(year, month, calendar.monthrange(year, month)[1]))
        dates = []
        if self.weekday is not None:
            day = first + dt.timedelta(days=(self.weekday - first.weekday()) % 7)
            while day <= until:
                dates.append(day)
                day += dt.timedelta(weeks=1)
            return dates
        year, month = first.year, first.month
        while (day := dt.date(year, month, min(self.day, calendar.monthrange(year, month)[1]))) <= until:
            dates.append(day)
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return dates

    def period_of(self, date: dt.date) -> tuple:
        """The period a movement of it is booked for: the month, or the ISO week if weekly."""
        return tuple(date.isocalendar()[:2]) if self.weekday is not None else (date.year, date.month)


def load_recurring(path: Path | None, snapshot: Snapshot) -> list[Recurring]:
    """
    The recurring movements of the YAML at `path` (none if missing or empty), checked against
    the base movements and accounts of the ledger. Raises ValueError if one is invalid.
    """
    if path is None or not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("The recurring YAML must map each recurring movement to its fields")  # noqa: TRY004 (a bad config value)
    return [_recurring(str(name), fields or {}, snapshot) for name, fields in data.items()]


def _recurring(name: str, fields: dict, snapshot: Snapshot) -> Recurring:
    what = f"Recurring movement {name!r}"
    if not isinstance(fields, dict):
        raise ValueError(f"{what} must have its fields")  # noqa: TRY004 (a bad config value)
    unknown = sorted(set(fields) - {"base_movement", "debit", "credit", "amount", "comment", "day", "weekday", "start", "end"})
    if unknown:
        raise ValueError(f"{what} has unknown fields: {unknown}")
    base = snapshot.ledger_base_movements().get(fields.get("base_movement"))
    if base is None or base.internal:
        raise ValueError(f"{what} needs the base_movement of a movement of the Ledger tab")
    sides = {}
    for side, key, fixed in ((LedgerSide.DEBIT, "debit", base.acc_debit), (LedgerSide.CREDIT, "credit", base.acc_credit)):
        given = fields.get(key)
        if fixed is not None:
            if given not in (None, fixed.name):
                raise ValueError(f"{what}: {base.name!r} always has {fixed.name!r} as its {key} account")
            sides[key] = fixed.name
        elif given not in {a.name for a in snapshot.side_accounts(base, side)}:
            raise ValueError(f"{what} needs a {key} account that {base.name!r} can take, not {given!r}")
        else:
            sides[key] = given
    day, weekday = fields.get("day"), fields.get("weekday")
    if (day is None) == (weekday is None):
        raise ValueError(f"{what} needs either a day of the month or a weekday")
    if day is not None and (not isinstance(day, int) or isinstance(day, bool) or not 1 <= day <= 31):
        raise ValueError(f"{what} needs a day of the month from 1 to 31, not {day!r}")
    if weekday is not None:
        if str(weekday).lower() not in WEEKDAYS:
            raise ValueError(f"{what} needs a weekday like 'monday', not {weekday!r}")
        weekday = WEEKDAYS.index(str(weekday).lower())
    start = _month(fields.get("start"), f"{what}: start") or store.today().strftime("%Y-%m")
    end = _month(fields.get("end"), f"{what}: end")
    if end and end < start:
        raise ValueError(f"{what} ends before it starts")
    amount = fields.get("amount")
    return Recurring(
        name, base.name, sides["debit"], sides["credit"],
        None if amount in (None, "") else _parse_amount(amount, f"amount of {name!r}"),
        str(fields.get("comment") or ""), day, weekday, start, end,
    )


def _month(value, what: str) -> str | None:
    """A YYYY-MM month (YAML may read it as text, or as a date if given a day)."""
    if value in (None, ""):
        return None
    text = value.strftime("%Y-%m") if isinstance(value, dt.date) else str(value)
    try:
        dt.datetime.strptime(text, "%Y-%m")
    except ValueError:
        raise ValueError(f"{what} must be a month like 2026-01, not {value!r}") from None
    return text


def due_movements(snapshot: Snapshot, recurring: list[Recurring]) -> list[dict]:
    """
    The movements of the recurring ones that are due (or upcoming, see UPCOMING) and not yet in
    the ledger CSV in their period, oldest first, with the values of the movement form.
    """
    now = store.today()
    booked: dict[tuple, set] = {}  # (base movement, debit, credit) -> the dates booked
    for row in snapshot.rows.itertuples():
        key = (row.base_movement, row.acc_debit, row.acc_credit)
        booked.setdefault(key, set()).add(dt.date.fromisoformat(row.move_date))
    due = []
    for r in recurring:
        periods = {r.period_of(d) for d in booked.get((r.base_movement, r.acc_debit, r.acc_credit), ())}
        for date in r.dates(now + dt.timedelta(days=UPCOMING)):
            if r.period_of(date) in periods:
                continue
            due.append({
                "name": r.name, "date": date.isoformat(), "in_days": (date - now).days,
                "every": "week" if r.weekday is not None else "month",
                "values": {
                    "move_date": date.isoformat(), "base_movement": r.base_movement,
                    "acc_debit": r.acc_debit, "acc_credit": r.acc_credit,
                    "amount": "" if r.amount is None else f"{r.amount:.2f}", "comment": r.comment,
                },
            })
    return sorted(due, key=lambda m: (m["date"], m["name"]))


def build_due(path: Path | None, snapshot: Snapshot) -> dict:
    """The due panel of the Ledger tab: the due movements, or what is wrong with the recurring YAML."""
    try:
        return {"due": due_movements(snapshot, load_recurring(path, snapshot)), "due_error": None}
    except (OSError, yaml.YAMLError, ValueError) as e:
        return {"due": [], "due_error": str(e)}
