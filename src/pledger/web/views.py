"""views.py"""

from __future__ import annotations

import datetime as dt
import math

from pledger.enums import AccountType, InvestmentType, LedgerSide, MovementType
from pledger.models import (
    SPECIAL_LABELS,
    DebtAccount,
    InvestmentAccount,
    next_month,
    special_of,
)
from pledger.web.store import (
    INITIAL_GAIN,
    Snapshot,
    Work,
    row_fingerprint,
    work_fingerprint,
)

ACCOUNT_TYPE_ORDER = list(AccountType)


def months_of(snapshot: Snapshot) -> list[str]:
    """Months with movements, newest first."""
    return sorted({d[:7] for d in snapshot.all_rows.move_date}, reverse=True)


def build_view(snapshot: Snapshot, month: str = "", account: str = "", initial: bool = False) -> dict:
    """Filtered movements and results for the ledger view, without the initial ones by default."""
    rows = snapshot.all_rows
    mask = rows.move_date.str.startswith(month) if month else rows.move_date.notna()
    if account:
        mask &= (rows.acc_debit == account) | (rows.acc_credit == account)
    if not initial:
        mask &= ~rows.base_movement.isin(snapshot.initial_base_names)
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
        "initial": initial,
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
    """Balances per account and per type, plus the state of the new account form."""
    rows = snapshot.all_rows
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
                "special": SPECIAL_LABELS.get(special_of(a), "-"),
                "balance": balances.get(a.name, 0.0),
            }
            for a in snapshot.accounts.values()
            if a.acc_type == t
        ]
        for t in ACCOUNT_TYPE_ORDER
    }
    totals = {t: sum(a["balance"] for a in accs) for t, accs in accounts_by_type.items()}

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
        # New equity accounts are not supported for now.
        "account_types": [t.value for t in ACCOUNT_TYPE_ORDER if t != AccountType.EQUITY],
        "currencies": list(snapshot.currencies.values()),
        "values": values,
    }


INVESTMENT_TYPE_LABELS = {
    InvestmentType.PUBLIC_BOND: "Public bond",
    InvestmentType.PRIVATE_BOND: "Private bond",
    InvestmentType.MUTUAL_FUND: "Mutual fund",
    InvestmentType.ETF: "ETF",
    InvestmentType.FOREX: "Forex",
    InvestmentType.STOCK: "Stock",
    InvestmentType.DERIVATIVE: "Derivative",
    InvestmentType.FIXED_TERM: "Fixed term",
}


def build_investments(snapshot: Snapshot) -> dict:
    """Investments held in each investment account, grouped by type and by account."""
    accounts = [a for a in snapshot.accounts.values() if isinstance(a, InvestmentAccount)]
    holdings = [
        {
            "account": a.name,
            "name": name,
            "type": INVESTMENT_TYPE_LABELS[h["investment"].type],
            "amount": h["total_amount"],
            "quantity": h["total_quantity"],
        }
        for a in accounts
        for name, h in a.investments.items()
    ]
    total = sum(h["amount"] for h in holdings)
    for h in holdings:
        h["share"] = round(100 * h["amount"] / total, 1) + 0.0 if total else 0.0

    # Types and accounts keep their defined order, listing only the ones with
    # investments. Investment names are only unique within an account.
    by_type = {
        t: {f"{h['name']} ({h['account']})": h["amount"] for h in holdings if h["type"] == t}
        for t in INVESTMENT_TYPE_LABELS.values()
        if any(h["type"] == t for h in holdings)
    }
    by_account = {
        a.name: {name: h["total_amount"] for name, h in a.investments.items()}
        for a in accounts
        if a.investments
    }
    return {
        "accounts": [
            {
                "name": a.name,
                "description": a.description,
                "currency": a.currency.name,
                "holdings": len(a.investments),
                "amount": sum(h["total_amount"] for h in a.investments.values()),
            }
            for a in accounts
        ],
        "holdings": holdings,
        "total": total,
        "groupings": {
            "type": grouped_composition(by_type),
            "account": grouped_composition(by_account),
        },
    }


def build_debts(snapshot: Snapshot) -> dict:
    """
    The people in each debt account: the ones we owe (creditors) and the ones
    that owe us (debtors), with what we owe and are owed as positive amounts,
    plus the debt of the movements whose comment names no one (unassigned).
    """
    accounts = [a for a in snapshot.accounts.values() if isinstance(a, DebtAccount)]
    people = [
        {
            "account": a.name,
            "name": c.name,
            "role": "Creditor" if c.is_creditor else "Debtor" if c.is_debtor else "Settled",
            "amount": c.amount,
        }
        for a in accounts
        for c in a.creditors.values()
    ] + [
        {"account": a.name, "name": "(no one)", "role": "Unassigned", "amount": a.unassigned}
        for a in accounts
        if not math.isclose(a.unassigned, 0.0, abs_tol=1e-9)
    ]

    def by_account(people_of, sign: int) -> list[dict]:
        return grouped_composition({
            a.name: {n: sign * c.amount for n, c in people_of(a).items()}
            for a in accounts
            if people_of(a)
        })

    we_owe = sum(c.amount for a in accounts for c in a.get_creditors().values())
    owed_to_us = -sum(c.amount for a in accounts for c in a.get_debtors().values())
    unassigned = sum(a.unassigned for a in accounts)
    return {
        "accounts": [
            {
                "name": a.name,
                "description": a.description,
                "currency": a.currency.name,
                "people": len(a.creditors),
                "net": sum(c.amount for c in a.creditors.values()) + a.unassigned,
            }
            for a in accounts
        ],
        "people": people,
        "we_owe": we_owe,
        "owed_to_us": owed_to_us,
        "unassigned": unassigned,
        "net": we_owe - owed_to_us + unassigned,
        "creditors": by_account(DebtAccount.get_creditors, 1),
        "debtors": by_account(DebtAccount.get_debtors, -1),
    }


def work_form(work: Work, month: str = "", values: dict | None = None) -> dict:
    """
    The state of the work month form: a month's values to edit it, or by default
    the month after the last one, at the same job.
    """
    by_month = {m.month: m for m in work.months}
    last = work.months[-1] if work.months else None
    if not month:
        month = next_month(last.month) if last else dt.date.today().strftime("%Y-%m")
    if values is None:
        m = by_month.get(month)
        if m is None:  # a new month, at the last job by default
            values = {"company": last.company if last else "", "position": last.position if last else ""}
        else:
            values = {
                "company": m.company or "",
                "position": m.position or "",
                "gross": "" if m.gross is None else f"{m.gross:,.2f}",
                "net": "" if m.net is None else f"{m.net:,.2f}",
                "payment_date": m.payment_date.isoformat() if m.payment_date else "",
            }
    return {
        "fields": {"month": month, **values},
        "editing": month in by_month,
        "company_names": list(work.companies),
        "position_names": sorted({p for c in work.companies.values() for p in c.positions}),
    }


def build_work(work: Work, form: dict | None = None) -> dict:
    """
    The companies worked for and their job positions, every month of the work
    history (the newest first) and the totals of this year, plus the form state.
    Worked months already over with no received payment date are flagged as unpaid.
    """
    today = dt.date.today()
    year, this_month = str(today.year), today.strftime("%Y-%m")
    this_year = [m for m in work.months if m.month[:4] == year and not m.is_gap]
    last_index = len(work.months) - 1
    months = [
        {
            "month": m,
            "index": i,
            "fingerprint": work_fingerprint(row),
            "deletable": i in {0, last_index},
            "unpaid": not m.is_gap and m.month < this_month and m.payment_date is None,
        }
        for i, (m, (_, row)) in enumerate(zip(work.months, work.rows.iterrows()))
    ][::-1]
    return {
        "companies": list(work.companies.values()),
        "current": work.current,
        "months": months,
        "unpaid": [r["month"].month for r in months if r["unpaid"]],
        "year": year,
        "gross": sum(m.gross for m in this_year),
        "net": sum(m.net for m in this_year),
        "form": form or work_form(work),
    }


def account_fields(snapshot: Snapshot, base_name: str) -> dict:
    """
    Which debit/credit accounts are fixed or selectable for a base movement: any
    non-equity account, or only investment or debt accounts on an INVESTMENT or
    DEBT side (with that side's label, e.g. "Investment").
    """
    base = snapshot.base_movements.get(base_name)
    free = [a.name for a in snapshot.free_accounts()]
    if base is None:
        return {
            "debit_fixed": None, "credit_fixed": None, "debit_options": free,
            "credit_options": free, "debit_special": None, "credit_special": None, "type": "",
        }
    return {
        "debit_fixed": base.acc_debit.name if base.acc_debit else None,
        "credit_fixed": base.acc_credit.name if base.acc_credit else None,
        "debit_options": [a.name for a in snapshot.side_accounts(base, LedgerSide.DEBIT)],
        "credit_options": [a.name for a in snapshot.side_accounts(base, LedgerSide.CREDIT)],
        "debit_special": SPECIAL_LABELS.get(base.debit_special),
        "credit_special": SPECIAL_LABELS.get(base.credit_special),
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
        "specials": SPECIAL_LABELS,
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


def build_charts(snapshot: Snapshot, month: str = "", initial: bool = False) -> dict:
    """
    Composition of balances, grouped by currency and by liquidity, and of results,
    and equity over time. Results leave out the initial movements by default.
    """
    rows = snapshot.all_rows
    months = months_of(snapshot)
    # Balances as of the end of the month; gains and losses within it.
    upto = rows[rows.move_date.str[:7] <= month] if month else rows
    balances = snapshot.ledger(upto).balances()
    in_period = rows.move_date.str.startswith(month) if month else rows.move_date.notna()
    if not initial:
        in_period &= ~rows.base_movement.isin(snapshot.initial_base_names)
    period = snapshot.ledger(rows[in_period])

    # Accounts, result types and base movements keep the order they are defined in.
    # Assets and liabilities are grouped by currency (base first) or by liquidity,
    # listing every group that has accounts.
    def grouped(acc_type: AccountType, key, groups: list) -> list[dict]:
        accs = [a for a in snapshot.accounts.values() if a.acc_type == acc_type]
        return grouped_composition({
            name: {a.name: balances.get(a.name, 0.0) for a in accs if key(a) == group}
            for group, name in groups
            if any(key(a) == group for a in accs)
        })

    by_currency = [(c, c) for c in snapshot.currencies]
    by_liquidity = list(LIQUIDITY_GROUPS.items())
    groupings = {
        "currency": {
            "assets": grouped(AccountType.ASSET, lambda a: a.currency.name, by_currency),
            "liabilities": grouped(AccountType.LIABILITY, lambda a: a.currency.name, by_currency),
        },
        "liquidity": {
            "assets": grouped(AccountType.ASSET, lambda a: a.is_liquid, by_liquidity),
            "liabilities": grouped(AccountType.LIABILITY, lambda a: a.is_liquid, by_liquidity),
        },
    }

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

    # Result statement: each result type in id order, with a running total.
    statement, running = [], 0.0
    for rt in result_types:
        gains = sum(results[MovementType.GAIN].get(rt.long_name, {}).values())
        losses = sum(results[MovementType.LOSS].get(rt.long_name, {}).values())
        running += gains - losses
        statement.append({
            "name": rt.long_name, "description": rt.description,
            "gains": gains, "losses": -losses, "net": gains - losses, "cumulative": running,
        })

    series = equity_series(snapshot, month, initial)
    equity = equity_chart(series)
    if equity:
        equity["change"] = sum(p["change"] for p in series)
        equity["since"] = "in the month" if month else f"since {series[0]['date']}"
    return {
        "months": months,
        "month": month,
        "initial": initial,
        "groupings": groupings,
        "gains": by_result_type(MovementType.GAIN),
        "losses": by_result_type(MovementType.LOSS),
        "statement": statement,
        "equity": equity,
        "equity_series": series,
    }
