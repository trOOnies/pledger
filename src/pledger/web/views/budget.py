"""budget.py: the Budget tab: the monthly budgets against what was spent, and the saving goals."""

from __future__ import annotations

import datetime as dt
from collections import Counter

from pandas import DataFrame

from pledger.enums import AccountType, MovementType
from pledger.models import rounds_to_zero
from pledger.web.store import Snapshot


def budgets_of(rows: DataFrame, month: str) -> dict[str, float]:
    """The monthly budget of each base movement in `month`: its last one up to then, if it isn't 0."""
    budgets: dict[str, float] = {}
    for row in rows[rows["month"] <= month].sort_values("month", kind="stable").to_dict("records"):
        budgets[row["base_movement"]] = float(row["amount"])
    return {base: amount for base, amount in budgets.items() if amount}


def spent_by_base(snapshot: Snapshot, month: str) -> dict[str, float]:
    """What the non internal loss base movements add up to in `month`, in the base currency."""
    rows = snapshot.all_rows
    moves = snapshot.ledger(rows[rows.move_date.str.startswith(month)]).movements
    spent: Counter = Counter()
    for m in moves:
        if m.move_type == MovementType.LOSS and not m.move_base.internal:
            spent[m.move_base.name] += m.amount
    return dict(spent)


def months_until(today: dt.date, date: dt.date) -> int:
    """The calendar months from today's to that of a date: 1 at least."""
    return max(1, (date.year - today.year) * 12 + date.month - today.month)


def build_budgets(snapshot: Snapshot, rows: DataFrame, month: str) -> dict:
    """
    The budget of each base movement in `month` against what was spent on it then, biggest
    budget first, with the spending left out of any budget.
    """
    spent = spent_by_base(snapshot, month)
    budgets = budgets_of(rows, month)
    items = [
        {
            "base": base, "budget": amount, "spent": spent.get(base, 0.0), "left": amount - spent.get(base, 0.0),
            "share": 100 * spent.get(base, 0.0) / amount, "over": spent.get(base, 0.0) - amount > 0.005,
        }
        for base, amount in sorted(budgets.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    budgeted = sum(i["budget"] for i in items)
    spent_budgeted = sum(i["spent"] for i in items)
    return {
        "month": month,
        "budgets": items,
        "budgeted": budgeted,
        "spent": spent_budgeted,
        "left": budgeted - spent_budgeted,
        "not_budgeted": sum(v for base, v in spent.items() if base not in budgets),
        "over": sum(i["over"] for i in items),
    }


def build_goals(snapshot: Snapshot, rows: DataFrame) -> dict:
    """
    Each saving goal, with what is saved: today's balance of its account, in its currency.
    Goals sharing an account each count all of it, so they are flagged. With a due date,
    what is left to save each month until then.
    """
    balances = snapshot.balances_today()
    records = rows.to_dict("records")
    per_account = Counter(g["account"] for g in records)
    goals = []
    for g in records:
        account = snapshot.accounts.get(g["account"])
        target, saved = float(g["target"]), balances.get(g["account"], 0.0)
        left = max(target - saved, 0.0)
        due = dt.date.fromisoformat(g["due"]) if g["due"] else None
        goals.append({
            "name": g["name"], "account": g["account"], "currency": account.currency.name if account else "",
            "target": target, "saved": saved, "left": left, "share": 100 * max(saved, 0.0) / target,
            "due": g["due"], "done": rounds_to_zero(left), "shared": per_account[g["account"]] > 1,
            "overdue": due is not None and due < snapshot.today and not rounds_to_zero(left),
            "monthly": left / months_until(snapshot.today, due) if due and due >= snapshot.today else None,
        })
    return {"goals": goals}


def budget_form_fields(snapshot: Snapshot) -> dict:
    """The base movements a budget can be of, and the accounts a saving goal can be on."""
    return {
        "loss_bases": sorted(snapshot.loss_base_movements()),
        "goal_accounts": [a.name for a in snapshot.accounts.values() if a.acc_type == AccountType.ASSET],
    }
