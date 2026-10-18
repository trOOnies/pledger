"""views.py"""

from __future__ import annotations

import datetime as dt
import math

from pledger.enums import AccountType, LedgerSide, MovementType
from pledger.models import INVESTMENT
from pledger.web.store import Snapshot, row_fingerprint

ACCOUNT_TYPE_ORDER = list(AccountType)


def months_of(snapshot: Snapshot) -> list[str]:
    """Months with movements, newest first."""
    return sorted({d[:7] for d in snapshot.rows.move_date}, reverse=True)


def build_view(snapshot: Snapshot, month: str = "", account: str = "") -> dict:
    """Filtered movements and results for the ledger view."""
    rows = snapshot.rows
    mask = rows.move_date.str.startswith(month) if month else rows.move_date.notna()
    if account:
        mask &= (rows.acc_debit == account) | (rows.acc_credit == account)
    filtered = rows[mask]
    ledger = snapshot.ledger(filtered)

    movements = [
        {
            "index": int(ix),
            "fingerprint": row_fingerprint(row),
            "date": move.move_date,
            "base": move.move_base.name,
            "type": move.move_type.value,
            "debit": move.acc_debit.name,
            "credit": move.acc_credit.name,
            "amount": move.amount,
            "comment": row.get("comment", ""),
            "internal": move.move_base.internal,
        }
        for (ix, row), move in zip(filtered.iterrows(), ledger.movements)
    ]
    movements.reverse()  # newest first

    return {
        "months": months_of(snapshot),
        "month": month,
        "account": account,
        "accounts": list(snapshot.accounts),
        "movements": movements,
        "gains": ledger.total_gains,
        "losses": ledger.total_losses,
        "profit": ledger.total_profits,
    }


LIQUIDITY_GROUPS = {True: "Liquid", False: "Illiquid"}


def grouped_composition(totals: dict[str, dict[str, float]]) -> list[dict]:
    """Ordered composition of groups, each with its rows as children, on the groups' scale."""
    rows = composition({g: sum(items.values()) for g, items in totals.items()}, ordered=True)
    if not rows:
        return rows
    scale = {"total": sum(r["value"] for r in rows), "top": max(r["value"] for r in rows)}
    for row in rows:
        row["children"] = composition(totals[row["name"]], ordered=True, **scale)
    return rows


def build_accounts(snapshot: Snapshot, month: str = "", values: dict | None = None) -> dict:
    """
    Balances per account and per type, grouped by currency and by liquidity,
    plus the state of the new account form.
    """
    rows = snapshot.rows
    # Balances are cumulative: as of the end of the selected month.
    upto = rows[rows.move_date.str[:7] <= month] if month else rows
    balances = snapshot.ledger(upto).balances()

    accounts_by_type = {
        t.value: [
            {
                "name": a.name,
                "description": a.description,
                "currency": a.currency.name,
                "is_liquid": a.is_liquid,
                "balance": balances.get(a.name, 0.0),
            }
            for a in snapshot.accounts.values()
            if a.acc_type == t
        ]
        for t in ACCOUNT_TYPE_ORDER
    }
    totals = {t: sum(a["balance"] for a in accs) for t, accs in accounts_by_type.items()}

    # Assets and liabilities grouped by currency (base first) or by liquidity,
    # listing every group that has accounts.
    def grouped(acc_type: AccountType, key: str, groups: list) -> list[dict]:
        accs = accounts_by_type[acc_type.value]
        return grouped_composition({
            name: {a["name"]: a["balance"] for a in accs if a[key] == group}
            for group, name in groups
            if any(a[key] == group for a in accs)
        })

    by_currency = [(c, c) for c in snapshot.currencies]
    by_liquidity = list(LIQUIDITY_GROUPS.items())
    groupings = {
        "currency": {
            "assets": grouped(AccountType.ASSET, "currency", by_currency),
            "liabilities": grouped(AccountType.LIABILITY, "currency", by_currency),
        },
        "liquidity": {
            "assets": grouped(AccountType.ASSET, "is_liquid", by_liquidity),
            "liabilities": grouped(AccountType.LIABILITY, "is_liquid", by_liquidity),
        },
    }

    values = values or {}
    values.setdefault("acc_type", AccountType.ASSET.value)
    values.setdefault("acc_currency", snapshot.base_currency.name)
    values.setdefault("acc_initial_amount", "0.00")
    return {
        "months": months_of(snapshot),
        "month": month,
        "assets": totals[AccountType.ASSET.value],
        "liabilities": totals[AccountType.LIABILITY.value],
        "equity": totals[AccountType.EQUITY.value],
        "accounts_by_type": accounts_by_type,
        "groupings": groupings,
        # New equity accounts are not supported for now.
        "account_types": [t.value for t in ACCOUNT_TYPE_ORDER if t != AccountType.EQUITY],
        "currencies": list(snapshot.currencies.values()),
        "values": values,
    }


def account_fields(snapshot: Snapshot, base_name: str) -> dict:
    """
    Which debit/credit accounts are fixed or selectable for a base movement: any
    non-equity account, or only investment accounts on an INVESTMENT side.
    """
    base = snapshot.base_movements.get(base_name)
    free = [a.name for a in snapshot.free_accounts()]
    if base is None:
        return {
            "debit_fixed": None, "credit_fixed": None, "debit_options": free,
            "credit_options": free, "debit_investment": False, "credit_investment": False, "type": "",
        }
    return {
        "debit_fixed": base.acc_debit.name if base.acc_debit else None,
        "credit_fixed": base.acc_credit.name if base.acc_credit else None,
        "debit_options": [a.name for a in snapshot.side_accounts(base, LedgerSide.DEBIT)],
        "credit_options": [a.name for a in snapshot.side_accounts(base, LedgerSide.CREDIT)],
        "debit_investment": base.debit_investment,
        "credit_investment": base.credit_investment,
        "type": base.move_type.value,
    }


def bases_by_type(snapshot: Snapshot) -> dict[str, list[str]]:
    """Names of the base movements that can be picked, grouped by movement type."""
    bases = snapshot.manual_base_movements()
    return {
        t.value: [n for n in sorted(bases) if bases[n].move_type == t]
        for t in MovementType
    }


def base_fields(snapshot: Snapshot, move_type: str = "", base_movement: str = "") -> dict:
    """
    Base movements to pick from for a movement type, and the account fields of
    the selected one. Without a type, it is taken from the base movement or
    defaults to the first type that has base movements.
    """
    by_type = bases_by_type(snapshot)
    base = snapshot.manual_base_movements().get(base_movement)
    if move_type not in by_type:
        move_type = (
            base.move_type.value if base
            else next((t for t, names in by_type.items() if names), MovementType.GAIN.value)
        )
    bases = by_type[move_type]
    if base_movement not in bases:
        base_movement = bases[0] if bases else ""
    return {
        "move_type": move_type,
        "bases": bases,
        "base_movement": base_movement,
        "fields": account_fields(snapshot, base_movement),
    }


def base_sides(snapshot: Snapshot, move_type: str) -> dict:
    """Which sides of a new base movement are equity (fixed) or selectable."""
    equity = next(
        (a.name for a in snapshot.accounts.values() if a.acc_type == AccountType.EQUITY), "Eq"
    )
    return {
        "debit_fixed": equity if move_type == MovementType.LOSS.value else None,
        "credit_fixed": equity if move_type == MovementType.GAIN.value else None,
        "free": [a.name for a in snapshot.free_accounts()],
        "investment": INVESTMENT,
        # Gains and losses are filed under a (non-internal) result type; transfers have none.
        "result_types": (
            []
            if move_type == MovementType.TRANSFER.value
            else [
                snapshot.result_types[i] for i in sorted(snapshot.result_types)
                if not snapshot.result_types[i].internal
            ]
        ),
    }


def setup_context(snapshot: Snapshot, values: dict | None = None) -> dict:
    """Result types and base movements, plus state of the setup forms."""
    values = values or {}
    values.setdefault("move_type", MovementType.LOSS.value)
    return {
        "result_types": [snapshot.result_types[i] for i in sorted(snapshot.result_types)],
        "bases_per_result_type": {
            rt.name: sum(b.result_type is rt for b in snapshot.base_movements.values())
            for rt in snapshot.result_types.values()
        },
        "bases_by_type": {
            t.value: [b for b in snapshot.base_movements.values() if b.move_type == t]
            for t in MovementType
        },
        "move_types": [t.value for t in MovementType],
        "sides": base_sides(snapshot, values["move_type"]),
        "values": values,
    }


def composition(
    totals: dict[str, float],
    limit: int | None = 8,
    total: float | None = None,
    top: float | None = None,
    ordered: bool = False,
) -> list[dict]:
    """
    Positive totals sorted by size, with their share of the whole. Past `limit`
    rows, the smallest ones are folded into a single "Other" row that keeps
    them under `rest`. `total` and `top` default to these totals' sum and
    largest row; pass the parent's to draw nested rows on the parent's scale.

    With `ordered`, every total is kept (zero or negative too) in the given
    order and nothing is folded.
    """
    if ordered:
        items = list(totals.items())
    else:
        items = sorted(((k, v) for k, v in totals.items() if v > 1e-9), key=lambda kv: -kv[1])
    rest = items[limit - 1:] if limit and not ordered and len(items) > limit else []
    if rest:
        items = [*items[:limit - 1], (f"Other ({len(rest)})", sum(v for _, v in rest))]
    if total is None:
        total = sum(v for _, v in items)
    if top is None:
        top = max((v for _, v in items), default=0.0)

    def row(name: str, value: float) -> dict:
        return {
            "name": name,
            "value": value,
            # Rounded so tiny negatives don't show as "-0.0%"
            "share": round(100 * value / total, 1) + 0.0 if total else 0.0,
            "width": 100 * max(value, 0.0) / top if top > 0 else 0.0,
        }

    rows = [row(k, v) for k, v in items]
    if rest:
        rows[-1]["rest"] = [row(k, v) for k, v in rest]
    return rows


def nice_ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    """Round axis ticks covering [lo, hi]."""
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    raw = (hi - lo) / count
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    ticks = [math.floor(lo / step) * step]
    while ticks[-1] < hi - 1e-9:
        ticks.append(ticks[-1] + step)
    return ticks


def compact(value: float) -> str:
    """Short axis label, e.g. 1.5M, 400K, -20."""
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= div:
            return f"{value / div:.3g}{suffix}"
    return f"{value:.3g}"


def equity_series(snapshot: Snapshot, month: str = "") -> list[dict]:
    """Equity at the end of each day with gains or losses (within `month`, if given)."""
    changes: dict[str, float] = {}
    for move in snapshot.ledger().movements:
        sign = {MovementType.GAIN: 1, MovementType.LOSS: -1}.get(move.move_type, 0)
        if sign:
            changes[move.move_date] = changes.get(move.move_date, 0.0) + sign * move.amount
    series, equity = [], 0.0
    for date in sorted(changes):
        equity += changes[date]
        if not month or date.startswith(month):
            series.append({"date": date, "value": equity, "change": changes[date]})
    return series


def equity_chart(series: list[dict]) -> dict | None:
    """
    Points, path and axes to draw the equity line. Coordinates are percentages
    of the plot area, so HTML labels and the SVG line share them.
    """
    if not series:
        return None
    dates = [dt.date.fromisoformat(p["date"]) for p in series]
    ticks = nice_ticks(min(p["value"] for p in series), max(p["value"] for p in series))
    y_lo, y_hi = ticks[0], ticks[-1]
    first, last = dates[0], dates[-1]
    span = max((last - first).days, 1)

    def x(d: dt.date) -> float:
        return 50.0 if first == last else round(100 * (d - first).days / span, 2)

    def y(v: float) -> float:
        return round(100 * (1 - (v - y_lo) / (y_hi - y_lo)), 2)

    points = [{**p, "x": x(d), "y": y(p["value"])} for p, d in zip(series, dates)]

    # Tick the start of each month, or the first and last day within a single month.
    month_starts = [
        dt.date(yr, mo, 1)
        for yr in range(first.year, last.year + 1)
        for mo in range(1, 13)
        if first <= dt.date(yr, mo, 1) <= last
    ]
    if len(month_starts) >= 2:
        every = math.ceil(len(month_starts) / 8)
        x_ticks = [{"x": x(m), "label": m.strftime("%b %Y")} for m in month_starts[::every]]
    else:
        x_ticks = [{"x": x(d), "label": d.strftime("%b %d")} for d in sorted({first, last})]
    return {
        "path": "M" + " L".join(f"{p['x']},{p['y']}" for p in points),
        "points": points,
        "y_ticks": [{"y": y(t), "label": compact(t)} for t in ticks],
        "x_ticks": x_ticks,
        "last": points[-1],
    }


def build_charts(snapshot: Snapshot, month: str = "") -> dict:
    """Composition of balances and results, and equity over time."""
    rows = snapshot.rows
    months = months_of(snapshot)
    # Balances as of the end of the month; gains and losses within it.
    upto = rows[rows.move_date.str[:7] <= month] if month else rows
    balances = snapshot.ledger(upto).balances()
    period = snapshot.ledger(rows[rows.move_date.str.startswith(month)] if month else rows)

    # Accounts, result types and base movements keep the order they are defined in.
    def by_account(acc_type: AccountType) -> list[dict]:
        return composition(
            {
                a.name: balances.get(a.name, 0.0)
                for a in snapshot.accounts.values()
                if a.acc_type == acc_type
            },
            ordered=True,
        )

    # Gains and losses per result type, then per base movement within each.
    period_totals: dict[str, float] = {}
    for move in period.movements:
        name = move.move_base.name
        period_totals[name] = period_totals.get(name, 0.0) + move.amount
    result_types = [snapshot.result_types[i] for i in sorted(snapshot.result_types)]
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

    # Result statement: each result type in id order, with a running total.
    statement, running = [], 0.0
    for rt_id in sorted(snapshot.result_types):
        rt = snapshot.result_types[rt_id]
        gains = sum(results[MovementType.GAIN].get(rt.long_name, {}).values())
        losses = sum(results[MovementType.LOSS].get(rt.long_name, {}).values())
        running += gains - losses
        statement.append({
            "name": rt.long_name, "description": rt.description,
            "gains": gains, "losses": -losses, "net": gains - losses, "cumulative": running,
        })

    series = equity_series(snapshot, month)
    equity = equity_chart(series)
    if equity:
        # A month is compared with the equity it started with; all time, with
        # the first entry (usually the opening balances).
        first = series[0]
        start = first["value"] - first["change"] if month else first["value"]
        equity["change"] = series[-1]["value"] - start
        equity["since"] = "since the start of the month" if month else f"since {first['date']}"
    return {
        "months": months,
        "month": month,
        "assets": by_account(AccountType.ASSET),
        "liabilities": by_account(AccountType.LIABILITY),
        "gains": by_result_type(MovementType.GAIN),
        "losses": by_result_type(MovementType.LOSS),
        "statement": statement,
        "equity": equity,
        "equity_series": series,
    }
