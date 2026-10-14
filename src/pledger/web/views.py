"""views.py"""

from __future__ import annotations

from pledger.enums import AccountType
from pledger.web.store import Snapshot, row_fingerprint

ACCOUNT_TYPE_ORDER = list(AccountType)


def build_view(snapshot: Snapshot, month: str = "", account: str = "") -> dict:
    """Filtered movements, totals and balances for the ledger view."""
    rows = snapshot.rows
    months = sorted({d[:7] for d in rows.move_date}, reverse=True)

    mask = rows.move_date.str.startswith(month) if month else rows.move_date.notna()
    if account:
        mask &= (rows.acc_debit == account) | (rows.acc_credit == account)
    filtered = rows[mask]
    ledger = snapshot.ledger(filtered)

    # Balances are cumulative: as of the end of the selected month.
    upto = rows[rows.move_date.str[:7] <= month] if month else rows
    balances = snapshot.ledger(upto).balances()

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
        }
        for (ix, row), move in zip(filtered.iterrows(), ledger.movements)
    ]
    movements.reverse()  # newest first

    accounts_by_type = {
        t.value: [
            {"name": a.name, "description": a.description, "balance": balances.get(a.name, 0.0)}
            for a in snapshot.accounts.values()
            if a.acc_type == t
        ]
        for t in ACCOUNT_TYPE_ORDER
    }
    return {
        "months": months,
        "month": month,
        "account": account,
        "accounts": list(snapshot.accounts),
        "movements": movements,
        "gains": ledger.total_gains,
        "losses": ledger.total_losses,
        "profit": ledger.total_profits,
        "accounts_by_type": accounts_by_type,
    }


def account_fields(snapshot: Snapshot, base_name: str) -> dict:
    """Which debit/credit accounts are fixed or selectable for a base movement."""
    base = snapshot.base_movements.get(base_name)
    free = [a.name for a in snapshot.free_accounts()]
    if base is None:
        return {"debit_fixed": None, "credit_fixed": None, "free": free, "type": ""}
    return {
        "debit_fixed": base.acc_debit.name if base.acc_debit else None,
        "credit_fixed": base.acc_credit.name if base.acc_credit else None,
        "free": free,
        "type": base.move_type.value,
    }
