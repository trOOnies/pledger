"""common.py: what the views of the tabs share: the tables of movements, compositions and month ends."""

from __future__ import annotations

import re

from pandas import DataFrame, Period

from pledger.enums import AccountType, MovementType
from pledger.ledger import Ledger
from pledger.models import next_month
from pledger.web.store import Snapshot, row_fingerprint

# The tab where each kind of movement that can't be deleted from the Ledger tab is
# handled: its colored circle there and its tooltip. The circle links to its row in
# that tab (see source_link).
MOVEMENT_SOURCES = {
    "investments": ("🟢", "Added, edited and deleted from the Investments tab"),
    "prices": ("🟢", "Generated from the prices of the investments and the ones bought and sold, in the Investments tab"),
    "currencies": ("🔵", "Added, edited and deleted from the Currencies tab"),
    "revaluation": ("🔵", "Generated from the values of the Currencies tab"),
    "work": ("🟠", "Generated each week from the salary of the Work tab"),
    "accounts": ("🟣", "Generated from the initial amount of the account, in the Accounts tab"),
    "corrections": ("🟣", "A balance correction, added from the Accounts tab; it can be deleted here"),
    "debt": ("🟡", "Added, edited and deleted from the Debt tab"),
    "services": ("🟡", "Generated from the monthly value of the service, in the Services tab"),
    "credit-cards": ("🔴", "Added, edited and deleted from the Credit Cards tab"),
}


def row_anchor(kind: str, key: str) -> str:
    """The HTML id of the row of a table that a link can jump to, e.g. movement-L-12."""
    return f"{kind}-" + re.sub(r"\s+", "_", key)


def source_link(source: str, move, move_id: str | None) -> str:
    """
    The link from a movement of the Ledger tab to its row in the tab of its `source`:
    the same movement, or the row it is generated from (the account revalued, the
    month of the salary, the account of the initial amount). Without an id yet, the tab.
    """
    if source == "revaluation":
        account = move.acc_debit.name if move.move_type == MovementType.GAIN else move.acc_credit.name
        return f"/currencies#{row_anchor('account', account)}"
    if source in {"accounts", "corrections"}:
        account = move.acc_debit.name if move.move_type == MovementType.GAIN else move.acc_credit.name
        return f"/accounts#{row_anchor('account', account)}"
    if source == "work":
        return f"/work#{row_anchor('month', move_id[2:9])}"  # W-<month>-<week>
    tab = {
        "investments": "/investments", "prices": "/investments", "currencies": "/currencies",
        "services": "/services",
        "debt": f"/debt?month={move.move_date[:7]}", "credit-cards": f"/credit-cards?month={move.move_date[:7]}",
    }[source]
    return f"{tab}#{row_anchor('movement', move_id)}" if move_id else tab


def ledger_link(move_id: str, move_date: str, initial: bool = False) -> str:
    """
    The link from a movement of another tab to its row in the Ledger tab, in its month
    and of every account, with the initial movements if it is one.
    """
    return f"/?month={move_date[:7]}{'&initial=true' if initial else ''}#{row_anchor('movement', move_id)}"


def months_of(snapshot: Snapshot) -> list[str]:
    """Months with movements, newest first."""
    return sorted({d[:7] for d in snapshot.all_rows.move_date}, reverse=True)


def movement_rows(snapshot: Snapshot, rows: DataFrame, ledger: Ledger) -> list[dict]:
    """The movements of a ledger built from these rows, newest first."""
    invest = snapshot.invest_base_movements()
    conversions = set(snapshot.conversion_rows.index)
    salaries = set(snapshot.salary_rows.index)
    revaluations = set(snapshot.result_rows.index)
    capital = set(snapshot.capital_rows.index)
    services = set(snapshot.service_rows.index)
    initials = set(snapshot.initial_rows.index)
    ledger_bases = snapshot.ledger_base_movements()
    debt_bases, card_bases = snapshot.debt_base_movements(), snapshot.card_payment_base_movements()
    corrections = snapshot.correction_base_movements()

    def source(ix, move) -> str | None:
        if ix in capital:
            return "prices"
        if move.move_base.name in invest:
            return "investments"
        if ix in conversions:
            return "currencies"
        if ix in revaluations:
            return "revaluation"
        if ix in salaries:
            return "work"
        if ix in services:
            return "services"
        if ix in initials:
            return "accounts"
        if move.move_base.name in debt_bases:
            return "debt"
        if move.move_base.name in card_bases:
            return "credit-cards"
        if move.move_base.name in corrections:
            return "corrections"
        return None

    movements = [
        {
            "index": int(ix),
            "id": snapshot.ids[ix],  # None for a CSV row added while pledger runs, until frozen
            "fingerprint": row_fingerprint(row),
            "date": move.move_date,
            "base": move.move_base.name,
            "type": move.move_type.value,
            "debit": move.acc_debit.name,
            "credit": move.acc_credit.name,
            "amount": move.amount,
            "comment": row.get("comment", ""),
            "internal": move.move_base.internal,
            "invest": move.move_base.name in invest,  # added and deleted from the Investments tab
            "conversion": ix in conversions,  # added and deleted from the Currencies tab
            "salary": ix in salaries,  # generated from the Work tab
            "source": source(ix, move),  # a key of MOVEMENT_SOURCES, if not deletable here
            # Can be edited from the movement form of the Ledger tab
            "editable": move.move_base.name in ledger_bases and not move.move_base.internal,
            # Can be deleted from the Ledger tab, though added from another tab
            "correction": move.move_base.name in corrections,
        }
        for (ix, row), move in zip(rows.iterrows(), ledger.movements)
    ]
    for m, move in zip(movements, ledger.movements):
        m["link"] = source_link(m["source"], move, m["id"]) if m["source"] else None
    movements.reverse()
    return movements


def grouped_composition(totals: dict[str, dict[str, float]]) -> list[dict]:
    """Ordered composition of groups, each with its rows as children, on the groups' scale."""
    rows = composition({g: sum(items.values()) for g, items in totals.items()}, ordered=True)
    if not rows:
        return rows
    scale = {"total": sum(r["value"] for r in rows), "top": max(r["value"] for r in rows)}
    for row in rows:
        row["children"] = composition(totals[row["name"]], ordered=True, **scale)
    return rows


def signed_composition(totals: dict[str, dict[str, float]]) -> list[dict]:
    """
    Groups with their rows as children, in order, where values can be negative (e.g.
    profits): their bars are as wide as their absolute value, all on the largest
    one's scale, and they have no share of a whole.
    """
    groups = {g: sum(items.values()) for g, items in totals.items()}
    top = max((abs(v) for items in (groups, *totals.values()) for v in items.values()), default=0.0)

    def rows(values: dict[str, float]) -> list[dict]:
        return [
            {"name": n, "value": v, "share": None, "width": 100 * abs(v) / top if top > 0 else 0.0}
            for n, v in values.items()
        ]

    return [{**row, "children": rows(totals[row["name"]])} for row in rows(groups)]


def account_movements(
    snapshot: Snapshot, names: list[str], month: str = "", editable: dict | None = None
) -> list[dict]:
    """
    The movements with any of these accounts on a side, newest first, in `month`
    (all the ledger by default), the initial movements included. Those of the ledger
    CSV of the `editable` base movements can be edited from the tab.
    """
    rows = snapshot.all_rows
    mask = rows.acc_debit.isin(names) | rows.acc_credit.isin(names)
    if month:
        mask &= rows.move_date.str.startswith(month)
    filtered = rows[mask]
    movements = movement_rows(snapshot, filtered, snapshot.ledger(filtered))
    # The balance of their account (of `names`) right after each one, in the whole ledger
    after = dict(zip(rows.index, snapshot.ledger(rows).running_balances()))
    for m in movements:
        m["initial"] = m["base"] in snapshot.initial_base_names
        m["editable"] = m["index"] >= 0 and m["base"] in (editable or {})
        m["balance"] = after[m["index"]][0 if m["debit"] in names else 1]
    return movements


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


def month_end(month: str) -> str:
    """The ISO date of the last day of a YYYY-MM month."""
    return Period(month, freq="M").end_time.date().isoformat()


def percent(value: float) -> str:
    """Axis label of a ratio, e.g. 0.25 -> 25%."""
    return f"{100 * value:.3g}%"


def month_end_balances(snapshot: Snapshot, month: str = "") -> list[tuple[str, dict[str, float]]]:
    """
    The balance of every account at the end of each month, from the first month of the
    ledger to the last one (or to `month`), signed like Ledger.balances.
    """
    moves = sorted(snapshot.ledger().movements, key=lambda m: str(m.move_date))
    if not moves:
        return []
    last = month or str(moves[-1].move_date)[:7]
    balances: dict[str, float] = {}
    out, i, cur = [], 0, str(moves[0].move_date)[:7]
    while cur <= last:
        while i < len(moves) and str(moves[i].move_date)[:7] <= cur:
            move = moves[i]
            for acc, sign in ((move.acc_debit, 1), (move.acc_credit, -1)):
                if acc.acc_type != AccountType.ASSET:
                    sign = -sign
                balances[acc.name] = balances.get(acc.name, 0.0) + sign * move.amount
            i += 1
        out.append((cur, dict(balances)))
        cur = next_month(cur)
    return out


def ratio_series(snapshot: Snapshot, monthly: list[tuple[str, dict[str, float]]], numerator) -> list[dict]:
    """
    A ratio over equity (assets minus liabilities) at the end of each month, from
    the month's `numerator(balances)`. None while equity isn't positive.
    """
    def total(balances: dict[str, float], acc_type: AccountType, liquid: bool | None = None) -> float:
        return sum(
            balances.get(a.name, 0.0) for a in snapshot.accounts.values()
            if a.acc_type == acc_type and liquid in (None, a.is_liquid)
        )

    series = []
    for month, balances in monthly:
        equity = total(balances, AccountType.ASSET) - total(balances, AccountType.LIABILITY)
        amount = numerator(balances, total)
        series.append({
            "date": f"{month}-01", "month": month, "amount": amount, "equity": equity,
            "value": amount / equity if equity > 1e-9 else None,
        })
    return series


AVERAGE_MONTH_DAYS = 365.25 / 12
