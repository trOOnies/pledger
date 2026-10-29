"""ledger.py: the Ledger tab: its movements, filters and search."""

from __future__ import annotations

import shlex

from pledger.web.store import Snapshot
from pledger.web.views.common import months_of, movement_rows


def search_terms(q: str) -> list[str]:
    """The terms of a search, case-folded: its words, or its "quoted phrases" as one term each."""
    try:
        terms = shlex.split(q)
    except ValueError:  # an unclosed quote (or an apostrophe, as in O'Brien): just the words
        terms = q.replace('"', " ").split()
    return [t.casefold() for t in terms if t.strip()]


def matches(movement: dict, terms: list[str]) -> bool:
    """
    Whether a movement has every term somewhere in its id, date, base movement, accounts,
    comment or amount (with two decimals, with or without thousands separators).
    """
    amount = movement["amount"]
    text = " ".join([
        movement["id"] or "", movement["date"], movement["base"], movement["debit"],
        movement["credit"], movement["comment"] or "", f"{amount:.2f}", f"{amount:,.2f}",
    ]).casefold()
    return all(t in text for t in terms)


def build_view(
    snapshot: Snapshot, month: str = "", account: str = "", initial: bool = False, base: str = "",
    q: str = "",
) -> dict:
    """
    Filtered movements (by month, account, base movement and a search, see matches) and
    results for the ledger view, without the initial ones by default.
    """
    rows = snapshot.all_rows
    mask = rows.move_date.str.startswith(month) if month else rows.move_date.notna()
    if account:
        mask &= (rows.acc_debit == account) | (rows.acc_credit == account)
    if base:
        mask &= rows.base_movement == base
    if not initial:
        mask &= ~rows.base_movement.isin(snapshot.initial_base_names)
    filtered = rows[mask]
    ledger = snapshot.ledger(filtered)
    movements = movement_rows(snapshot, filtered, ledger)
    terms = search_terms(q)
    if terms:
        movements = [m for m in movements if matches(m, terms)]
        filtered = filtered.loc[[m["index"] for m in reversed(movements)]]
        ledger = snapshot.ledger(filtered)
    # The balances after each movement are those of the whole ledger, whatever the filters.
    after = dict(zip(rows.index, snapshot.ledger(rows).running_balances()))
    for m, ix in zip(movements, filtered.index[::-1]):
        m["debit_balance"], m["credit_balance"] = after[ix]

    return {
        "months": months_of(snapshot),
        "month": month,
        "account": account,
        "initial": initial,
        "accounts": list(snapshot.accounts),
        "base": base,
        "bases": list(snapshot.base_movements),
        "q": q,
        "movements": movements,
        # Not shown in the Ledger tab, but in /api/summary
        "gains": ledger.total_gains,
        "losses": ledger.total_losses,
        "profit": ledger.total_profits,
    }
