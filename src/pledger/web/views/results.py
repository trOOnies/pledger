"""results.py: the Results tab: gains, losses and profit by result type, and equity over time."""

from __future__ import annotations

import datetime as dt
from bisect import bisect_left, bisect_right
from itertools import accumulate

from pandas import Timedelta, Timestamp

from pledger.enums import MovementType
from pledger.models import CPI_BASE, next_month
from pledger.web.store import INITIAL_GAIN, Snapshot
from pledger.web.views.charts import (
    column_chart,
    line_chart,
    stacked_column_chart,
    waterfall_chart,
    with_real,
)
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


def build_results(
    snapshot: Snapshot, month: str = "", initial: bool = False, expense_types: list[str] | None = None,
) -> dict:
    """
    Composition of results, the result statement, equity over time and equity in months
    of the expenses of the `expense_types` (see expense_months). Results leave out the
    initial movements by default. Result types and base movements keep the order they are defined in.
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

    internal = {rt.long_name for rt in result_types if rt.internal}

    def marked(groups: list[dict]) -> list[dict]:
        # The internal result types are marked as such.
        return [{**g, "internal": g["name"] in internal} for g in groups]

    def by_result_type(move_type: MovementType) -> list[dict]:
        # Every result type and base movement is listed, even at zero in this period.
        return marked(grouped_composition(results[move_type]))

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
            "name": rt.long_name, "short": rt.name, "description": rt.description, "internal": rt.internal,
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
        "profits": marked(signed_composition(profits)),
        # The same, each result type summed to the ones before it
        "cumulative_results": [
            cumulative_composition(c)
            for c in (by_result_type(MovementType.GAIN), by_result_type(MovementType.LOSS), marked(signed_composition(profits)))
        ],
        "statement": statement,
        "waterfall": waterfall_chart(statement),
        "relative": relative,
        "equity": equity,
        "equity_series": series,
        "expenses": expense_months(snapshot, month, initial, expense_types),
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


def loss_result_types(snapshot: Snapshot, initial: bool = False) -> list:
    """The result types with loss base movements, in order: the initial one only with `initial`."""
    initial_rt = snapshot.base_movements[INITIAL_GAIN].result_type
    return [
        rt for rt in (snapshot.result_types[i] for i in sorted(snapshot.result_types))
        if (initial or rt is not initial_rt)
        and any(b.result_type is rt and b.move_type == MovementType.LOSS for b in snapshot.base_movements.values())
    ]


def expense_months(
    snapshot: Snapshot, month: str = "", initial: bool = False, selected: list[str] | None = None,
) -> dict:
    """
    Equity in months of expenses at the end of each month of the selection (`month`, else
    every month of the ledger): its equity then over the average monthly losses of the
    `selected` loss result types (their names; the non-internal ones by default), each the
    losses of its base movements in the EXPENSES_DAYS up to then, like equity_metrics.
    The `chart` draws those months, and the `raw_chart` the averages themselves, stacked
    by result type. A month without expenses has no months (None), drawn at 0.
    """
    types = loss_result_types(snapshot, initial)
    if selected is None:
        selected = [rt.name for rt in types if not rt.internal]
    chosen = [rt for rt in types if rt.name in set(selected)]
    slots = {rt.name: i % 8 + 1 for i, rt in enumerate(types)}
    filters = [{"name": rt.name, "long_name": rt.long_name, "internal": rt.internal, "slot": slots[rt.name],
                "checked": rt in chosen} for rt in types]
    empty = {"filters": filters, "months": [], "chart": None, "raw_chart": None, "selected": [rt.name for rt in chosen]}
    dates = snapshot.all_rows.move_date
    if not len(dates):
        return empty

    # The change of equity and the losses of each result type of each date, summed up to it
    by_rt = {rt.name: rt for rt in types}
    equity: dict[str, float] = {}
    losses: dict[str, dict[str, float]] = {rt.name: {} for rt in types}
    for move in snapshot.ledger().movements:
        sign = {MovementType.GAIN: 1, MovementType.LOSS: -1}.get(move.move_type, 0)
        if not sign:
            continue
        date = str(move.move_date)
        equity[date] = equity.get(date, 0.0) + sign * move.amount
        rt = move.move_base.result_type
        if sign < 0 and rt is not None and by_rt.get(rt.name) is rt:
            losses[rt.name][date] = losses[rt.name].get(date, 0.0) + move.amount
    days = sorted(equity)
    equity_upto = list(accumulate(equity[d] for d in days))
    losses_upto = {n: list(accumulate(by_date.get(d, 0.0) for d in days)) for n, by_date in losses.items()}

    def upto(cumulative: list[float], date: str) -> float:
        i = bisect_right(days, date)
        return cumulative[i - 1] if i else 0.0

    def between(cumulative: list[float], start: str, end: str) -> float:
        i, j = bisect_left(days, start), bisect_right(days, end)
        return (cumulative[j - 1] if j else 0.0) - (cumulative[i - 1] if i else 0.0)

    first, today = dates.min(), dt.date.today().isoformat()
    if month:
        months = [month]
    else:
        months, m = [], first[:7]
        while m <= dates.max()[:7]:
            months.append(m)
            m = next_month(m)
    rows = []
    for m in months:
        end = max(min(month_end(m), today), f"{m}-01")
        window = max((dt.date.fromisoformat(end) - dt.timedelta(days=EXPENSES_DAYS - 1)).isoformat(), first)
        span = ((dt.date.fromisoformat(end) - dt.date.fromisoformat(window)).days + 1) / AVERAGE_MONTH_DAYS
        averages = {rt.name: between(losses_upto[rt.name], window, end) / span for rt in chosen} if span > 0 else {}
        monthly = sum(averages.values())
        end_equity = upto(equity_upto, end)
        rows.append({
            "month": m, "end": end, "equity": end_equity, "averages": averages, "monthly": monthly,
            "months": end_equity / monthly if monthly > 1e-9 else None,
        })

    chart = column_chart([{"month": r["month"], "value": r["months"] or 0.0} for r in rows], tick_label=lambda t: f"{t:g}")
    raw_chart = stacked_column_chart(
        [{"month": r["month"], "values": {by_rt[n].long_name: v for n, v in r["averages"].items()}} for r in rows],
        {rt.long_name: slots[rt.name] for rt in chosen},
    )
    return {**empty, "months": rows, "chart": chart, "raw_chart": raw_chart}
