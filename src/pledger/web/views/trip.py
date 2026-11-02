"""trip.py: the Trip tab: the planned expenses of a trip against the ones in the ledger."""

from __future__ import annotations

import datetime as dt

from pandas import DataFrame

from pledger.web.store import Snapshot
from pledger.web.store.common import _value_on
from pledger.web.views.budget import months_until
from pledger.web.views.common import movement_rows

TRIP_CATEGORIES = ["Flights", "Lodging", "Transport", "Food", "Activities", "Insurance", "Other"]


def ledger_weeks(start: str, end: str) -> tuple[str, str]:
    """
    The dates of the movements of a trip: the ledger can date its movements on the Monday of
    their week, so from the Monday of the week it starts to the first Monday after it ends.
    """
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    return (
        (first - dt.timedelta(days=first.weekday())).isoformat(),
        (last + dt.timedelta(days=-last.weekday() % 7)).isoformat(),
    )


def trip_movements(snapshot: Snapshot, trip: dict) -> list[dict]:
    """The movements of the base movement of a trip in its ledger weeks, newest first."""
    if not trip["base_movement"]:
        return []
    first, last = ledger_weeks(trip["start"], trip["end"])
    rows = snapshot.all_rows
    rows = rows[(rows.base_movement == trip["base_movement"]) & (rows.move_date >= first) & (rows.move_date <= last)]
    return movement_rows(snapshot, rows, snapshot.ledger(rows))


def in_base(snapshot: Snapshot, amount: float, currency: str) -> float | None:
    """An amount in the base currency at today's value, None without a value of its currency."""
    found = snapshot.currencies.get(currency)
    if found is None:
        return None
    try:
        return amount * _value_on(found, snapshot.today.isoformat(), "to plan an expense in it")
    except ValueError:
        return None


def trip_status(trip: dict, today: dt.date) -> str:
    if trip["start"] > today.isoformat():
        return "Upcoming"
    return "Done" if trip["end"] < today.isoformat() else "Under way"


def pick_trip(trips: list[dict], name: str, today: dt.date) -> dict | None:
    """The trip asked for, else the first one not over yet, else the last one."""
    by_start = sorted(trips, key=lambda t: t["start"])
    return (
        next((t for t in trips if t["name"] == name), None)
        or next((t for t in by_start if t["end"] >= today.isoformat()), None)
        or (by_start[-1] if by_start else None)
    )


def build_trips(snapshot: Snapshot, trip_rows: DataFrame, item_rows: DataFrame, name: str = "") -> dict:
    """
    Every trip with what was planned, in the base currency at today's values, and spent, and
    the one picked with its planned expenses, by category too, and its movements. Before it
    starts, also what is left to set aside each month.
    """
    items = []
    for row in item_rows.to_dict("records"):
        amount = float(row["amount"])
        items.append({**row, "amount": amount, "base": in_base(snapshot, amount, row["currency"])})

    trips = []
    for row in trip_rows.to_dict("records"):
        moves = trip_movements(snapshot, row)
        planned = sum(i["base"] or 0.0 for i in items if i["trip"] == row["name"])
        spent = sum(m["amount"] for m in moves)
        days = (dt.date.fromisoformat(row["end"]) - dt.date.fromisoformat(row["start"])).days + 1
        trips.append({
            **row, "days": days, "planned": planned, "spent": spent, "left": planned - spent,
            "per_day": planned / days, "status": trip_status(row, snapshot.today), "movements": moves,
        })

    trip = pick_trip(trips, name, snapshot.today)
    mine = [i for i in items if trip and i["trip"] == trip["name"]]
    categories = []
    if trip:
        totals: dict[str, float] = {}
        for i in mine:
            totals[i["category"] or "Other"] = totals.get(i["category"] or "Other", 0.0) + (i["base"] or 0.0)
        categories = [
            {"name": c, "value": v, "share": 100 * v / trip["planned"] if trip["planned"] else 0.0}
            for c, v in sorted(totals.items(), key=lambda kv: -kv[1])
        ]
        start = dt.date.fromisoformat(trip["start"])
        trip["monthly"] = (
            max(trip["left"], 0.0) / months_until(snapshot.today, start) if start > snapshot.today else None
        )
    return {
        "trips": sorted(trips, key=lambda t: t["start"], reverse=True),
        "trip": trip,
        "items": mine,
        "no_value": sorted({i["currency"] for i in mine if i["base"] is None}),
        "categories": categories,
        "category_names": TRIP_CATEGORIES,
        "currencies": list(snapshot.currencies),
        "base_currency": snapshot.base_currency.name,
        "loss_bases": sorted(snapshot.loss_base_movements()),
        "default_base": next((n for n in sorted(snapshot.loss_base_movements()) if "trip" in n.lower()), ""),
    }
