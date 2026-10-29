"""results.py: the Results tab: gains, losses and profit by result type, and equity over time."""

from __future__ import annotations

import datetime as dt

from pandas import Timedelta, Timestamp

from pledger.enums import MovementType
from pledger.models import CPI_BASE
from pledger.web.store import INITIAL_GAIN, Snapshot
from pledger.web.views.charts import line_chart, waterfall_chart, with_real
from pledger.web.views.common import (
    AVERAGE_MONTH_DAYS,
    grouped_composition,
    month_end,
    months_of,
    percent,
    signed_composition,
)


def cumulative_composition(groups: list[dict]) -> list[dict]:
    """
    The groups of a composition (see grouped_composition and signed_composition), each
    one's value summed to the ones before it, and their children as they are, all on
    the scale of the largest absolute value. Their share is of the last one's value.
    """
    running, summed = 0.0, []
    for g in groups:
        running += g["value"]
        summed.append({**g, "value": running})
    top = max((abs(r["value"]) for g in summed for r in (g, *g.get("children", []))), default=0.0)

    def scaled(row: dict) -> dict:
        return {**row, "width": 100 * abs(row["value"]) / top if top > 0 else 0.0}

    return [
        {
            **scaled(g),
            "share": (round(100 * g["value"] / running, 1) + 0.0 if running else 0.0) if g["share"] is not None else None,
            "children": [scaled(c) for c in g.get("children", [])],
        }
        for g in summed
    ]


def equity_series(snapshot: Snapshot, month: str = "", initial: bool = False) -> list[dict]:
    """
    Equity at the end of each day with gains or losses (within `month`, if given).
    Initial movements always add to equity, but only count as changes with `initial`.
    """
    skipped = set() if initial else snapshot.initial_base_names
    totals: dict[str, float] = {}  # every gain and loss
    changes: dict[str, float] = {}  # the ones shown as changes
    for move in snapshot.ledger().movements:
        sign = {MovementType.GAIN: 1, MovementType.LOSS: -1}.get(move.move_type, 0)
        if sign:
            totals[move.move_date] = totals.get(move.move_date, 0.0) + sign * move.amount
            if move.move_base.name not in skipped:
                changes[move.move_date] = changes.get(move.move_date, 0.0) + sign * move.amount
    series, equity = [], 0.0
    for date in sorted(totals):
        equity += totals[date]
        if date in changes and (not month or date.startswith(month)):
            series.append({"date": date, "value": equity, "change": changes[date]})
    return series


def build_results(snapshot: Snapshot, month: str = "", initial: bool = False) -> dict:
    """
    Composition of results, the result statement and equity over time. Results leave
    out the initial movements by default. Result types and base movements keep the
    order they are defined in.
    """
    rows = snapshot.all_rows
    # Gains and losses within the month.
    in_period = rows.move_date.str.startswith(month) if month else rows.move_date.notna()
    if not initial:
        in_period &= ~rows.base_movement.isin(snapshot.initial_base_names)
    period = snapshot.ledger(rows[in_period])

    # Gains and losses per result type, then per base movement within each.
    period_totals: dict[str, float] = {}
    for move in period.movements:
        name = move.move_base.name
        period_totals[name] = period_totals.get(name, 0.0) + move.amount
    initial_rt = snapshot.base_movements[INITIAL_GAIN].result_type
    result_types = [
        snapshot.result_types[i] for i in sorted(snapshot.result_types)
        if initial or snapshot.result_types[i] is not initial_rt
    ]
    results: dict[MovementType, dict[str, dict[str, float]]] = {
        move_type: {
            rt.long_name: {
                b.name: period_totals.get(b.name, 0.0)
                for b in snapshot.base_movements.values()
                if b.result_type is rt and b.move_type == move_type
            }
            for rt in result_types
        }
        for move_type in (MovementType.GAIN, MovementType.LOSS)
    }

    def by_result_type(move_type: MovementType) -> list[dict]:
        # Every result type and base movement is listed, even at zero in this period.
        return grouped_composition(results[move_type])

    # The profit of each result type: its gains minus its losses, base movement by base movement.
    profits = {
        rt.long_name: {
            **results[MovementType.GAIN][rt.long_name],
            **{b: -v for b, v in results[MovementType.LOSS][rt.long_name].items()},
        }
        for rt in result_types
    }

    # Result statement: each result type in id order, with a running total.
    statement, running = [], 0.0
    for rt in result_types:
        gains = sum(results[MovementType.GAIN].get(rt.long_name, {}).values())
        losses = sum(results[MovementType.LOSS].get(rt.long_name, {}).values())
        running += gains - losses
        statement.append({
            "name": rt.long_name, "short": rt.name, "description": rt.description,
            "gains": gains, "losses": -losses, "net": gains - losses, "cumulative": running,
        })

    # The same statement over the gains of the first result type that isn't internal (e.g.
    # the operative one), if any in this period.
    first = next((r for r, rt in zip(statement, result_types) if not rt.internal), None)
    relative = None
    if first and first["gains"] > 1e-9:
        over = [
            {**r, **{k: r[k] / first["gains"] for k in ("gains", "losses", "net", "cumulative")}}
            for r in statement
        ]
        relative = {"name": first["name"], "statement": over, "waterfall": waterfall_chart(over, percent)}

    series = equity_series(snapshot, month, initial)
    real_date = with_real(series, snapshot.base_currency.customer_price_index)
    equity = line_chart(series)
    if equity:
        equity["change"] = sum(p["change"] for p in series)
        equity["since"] = "in the month" if month else f"since {series[0]['date']}"
        equity["real_date"] = real_date
    return {
        "months": months_of(snapshot),
        "month": month,
        "initial": initial,
        **equity_metrics(snapshot, month),
        "gains": by_result_type(MovementType.GAIN),
        "losses": by_result_type(MovementType.LOSS),
        "profits": signed_composition(profits),
        # The same, each result type summed to the ones before it
        "cumulative_results": [
            cumulative_composition(c)
            for c in (by_result_type(MovementType.GAIN), by_result_type(MovementType.LOSS), signed_composition(profits))
        ],
        "statement": statement,
        "waterfall": waterfall_chart(statement),
        "relative": relative,
        "equity": equity,
        "equity_series": series,
    }


EXPENSES_DAYS = 365  # the expenses covered by equity are the average of the losses of this many days


def equity_metrics(snapshot: Snapshot, month: str = "") -> dict:
    """
    How much equity changed in `month` (all the ledger by default), nominal and real,
    and how many months of expenses it covers at its end.

    The change goes from the equity at the start of the period, plus the initial
    amounts of the accounts starting in it (they aren't a change), to its equity at the
    end: the end of the month (up to today), else the last date of the ledger. The real
    change deflates both by the CPI of the base currency at their dates (None without one).
    The expenses are the average monthly losses of the EXPENSES_DAYS up to the end of the
    period, of the non-internal base movements but the deductions of the salary, which
    would stop with it.
    """
    none = {"equity_change": None, "equity_real_change": None, "months_covered": None, "monthly_expenses": None}
    dates = snapshot.all_rows.move_date
    if not len(dates):
        return none
    today = dt.date.today().isoformat()
    start = f"{month}-01" if month else dates.min()
    end = max(min(month_end(month), today), start) if month else dates.max()
    initial = snapshot.initial_base_names
    deductions = set(snapshot.work.deductions)
    start_equity = end_equity = expenses = 0.0
    window = (dt.date.fromisoformat(end) - dt.timedelta(days=EXPENSES_DAYS - 1)).isoformat()
    window = max(window, dates.min())
    for move in snapshot.ledger().movements:
        sign = {MovementType.GAIN: 1, MovementType.LOSS: -1}.get(move.move_type, 0)
        if not sign or move.move_date > end:
            continue
        end_equity += sign * move.amount
        if move.move_date < start or move.move_base.name in initial:
            start_equity += sign * move.amount
        if (
            sign < 0 and move.move_date >= window and not move.move_base.internal
            and move.move_base.name not in deductions
        ):
            expenses += move.amount
    metrics = dict(none)
    if start_equity:
        metrics["equity_change"] = (end_equity - start_equity) / abs(start_equity)
    cpi = snapshot.base_currency.customer_price_index
    if start_equity and cpi is not None and len(cpi):
        before = Timestamp(start) - Timedelta(days=1)
        cpi_start = CPI_BASE if before < cpi.index[0] == Timestamp(start) else cpi.get(before)
        cpi_end = cpi.get(Timestamp(end))
        if cpi_start is not None and cpi_end is not None:
            real_start, real_end = start_equity / cpi_start, end_equity / cpi_end
            metrics["equity_real_change"] = (real_end - real_start) / abs(real_start)
    days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(window)).days + 1
    monthly = expenses / (days / AVERAGE_MONTH_DAYS)
    if monthly > 0:
        metrics["monthly_expenses"] = monthly
        metrics["months_covered"] = end_equity / monthly
    return metrics
