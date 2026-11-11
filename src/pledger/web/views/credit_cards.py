"""credit_cards.py: the Credit Cards tab: what is owed and charged, and the card payment form."""

from __future__ import annotations

import datetime as dt

from pledger.models import (
    CREDIT_CARD,
    CreditCardAccount,
    add_months,
    is_foreign,
    next_month,
    rounds_to_zero,
)
from pledger.web.store import (
    CARD_PAYMENT,
    CARD_PAYMENT_BASES,
    CARD_SURPLUS,
    Snapshot,
    installments_of,
)
from pledger.web.views.charts import column_chart, line_chart, with_real
from pledger.web.views.common import (
    account_movements,
    composition,
    month_end,
    month_end_balances,
    months_of,
    movement_rows,
    own_balance,
    percent,
    ratio_series,
)
from pledger.web.views.forms import account_fields


def build_credit_cards(snapshot: Snapshot, month: str = "", card: str = "") -> dict:
    """
    The balance of each credit card account (what we owe on it) as of the end of
    `month` (all the ledger by default), and what was charged to it (credits) and
    paid (debits) in that month, leaving out the initial movements. Lists the
    cards with a negative balance (we are owed): rare, so possibly an error. Charts
    the net owed on them at each month end, nominal, real and over equity, and what
    was charged to them in each month, up to `month`. A card in another currency also
    has its balance in that currency, in `own` (None for the others). With the statement
    closing dates of each card in the year of `month` (of today for all time).
    """
    cards = [a for a in snapshot.accounts.values() if isinstance(a, CreditCardAccount)]
    if card not in {a.name for a in cards}:
        card = ""
    rows = snapshot.all_rows
    balances = snapshot.ledger(rows[rows.move_date.str[:7] <= month] if month else rows).balances()
    in_month = rows[~rows.base_movement.isin(snapshot.initial_base_names)]
    if month:
        in_month = in_month[in_month.move_date.str.startswith(month)]
    moves = snapshot.ledger(in_month).movements

    accounts = [
        {
            "name": a.name,
            "description": a.description,
            "currency": a.currency.name,
            "charged": sum(m.amount for m in moves if m.acc_credit is a),
            "paid": sum(m.amount for m in moves if m.acc_debit is a),
            "balance": balances.get(a.name, 0.0),
            "own": own_balance(snapshot, a, month),
        }
        for a in cards
    ]
    return {
        "accounts": accounts,
        "has_own": any(a["own"] is not None for a in accounts),
        "months": months_of(snapshot),
        "month": month,
        "base_currency": snapshot.base_currency.name,
        "owed": sum(a["balance"] for a in accounts),
        "charged": sum(a["charged"] for a in accounts),
        "paid": sum(a["paid"] for a in accounts),
        "composition": composition({a["name"]: a["balance"] for a in accounts}, ordered=True),
        "negative": [a for a in accounts if a["balance"] < 0 and not rounds_to_zero(a["balance"])],
        # Of the card picked in the filter, else of every card
        "movements": account_movements(
            snapshot, [card] if card else [a.name for a in cards], month,
            editable=snapshot.card_payment_base_movements(),
        ),
        "card": card,
        **card_installments(snapshot, [card] if card else [a.name for a in cards], month),
        **owed_charts(snapshot, [a.name for a in cards], month),
        **charged_chart(snapshot, [a.name for a in cards], month),
        **paid_chart(snapshot, [a.name for a in cards], month),
        **closing_dates(snapshot, month[:4]),
    }


def closing_dates(snapshot: Snapshot, year: int | str | None = None) -> dict:
    """
    The statement closing dates of each credit card for each month of a year (of today
    by default): the ones set by hand, and the others autocompleted from them (see
    CreditCardAccount.closing_date). The years to pick from go from the first one of the
    ledger or of a closing date to the year after today.
    """
    cards = [a for a in snapshot.accounts.values() if isinstance(a, CreditCardAccount)]
    today = snapshot.today
    first = [int(d[:4]) for d in snapshot.all_rows.move_date[:1]]
    set_years = [int(m[:4]) for a in cards for m in a.closing_dates]
    years = list(range(min([today.year, *first, *set_years]), max([today.year + 1, *set_years]) + 1))
    try:
        year = int(year) if year else today.year
    except ValueError:
        year = today.year
    if year not in years:
        year = today.year
    months = [f"{year}-{m:02d}" for m in range(1, 13)]
    return {
        "closing_year": year,
        "closing_years": years,
        "closing_months": months,
        "closing_cards": [
            {
                "name": a.name,
                "description": a.description,
                "any_set": bool(a.closing_dates),
                "months": [
                    {"month": m, "date": a.closing_date(m).isoformat(), "set": a.is_closing_set(m)}
                    for m in months
                ],
            }
            for a in cards
        ],
    }


def card_charges(snapshot: Snapshot, cards: list[str]):
    """The rows of the charges to these credit cards: their credits, leaving out the initial movements."""
    rows = snapshot.all_rows
    return rows[rows.acc_credit.isin(cards) & ~rows.base_movement.isin(snapshot.initial_base_names)]


def charge_statements(snapshot: Snapshot, cards: list[str]) -> list[tuple[int, str, CreditCardAccount, float, int]]:
    """
    The charges to the credit cards (see card_charges): the index of the row of each, the
    statement month it is billed in (see CreditCardAccount.statement_month), its card, its
    amount and its number of installments.
    """
    charges = card_charges(snapshot, cards)
    found = []
    for (ix, row), move in zip(charges.iterrows(), snapshot.ledger(charges).movements):
        card = move.acc_credit
        date = dt.date.fromisoformat(str(move.move_date)[:10])
        found.append((int(ix), card.statement_month(date), card, move.amount, installments_of(snapshot, row)))
    return found


def card_installments(snapshot: Snapshot, cards: list[str], month: str = "") -> dict:
    """
    The installments of the charges to these credit cards (see paid_chart), of the statement
    `month` (all of them by default), newest statement first: each with its charge (its ID,
    date, base movement and comment), its card, statement month and closing date, its number
    out of how many, its amount and whether it is made (its statement closed before today)
    or still due. With their totals made and due.
    """
    charges = card_charges(snapshot, cards)
    moves = {m["index"]: m for m in movement_rows(snapshot, charges, snapshot.ledger(charges))}
    today = snapshot.today
    found = []
    for ix, statement, card, amount, n in charge_statements(snapshot, cards):
        move = moves[ix]
        for i, value in enumerate(installments(amount, n)):
            when = add_months(statement, i)
            if month and when != month:
                continue
            closes = card.closing_date(when)
            found.append({
                "id": move["id"], "date": move["date"], "base": move["base"], "type": move["type"],
                "comment": move["comment"], "card": card.name, "statement": when,
                "closes": closes.isoformat(), "number": i + 1, "of": n, "amount": value,
                "made": closes < today,
            })
    found.sort(key=lambda p: (p["statement"], str(p["date"]), p["number"]), reverse=True)
    return {
        "installments": found,
        "installments_made": sum(p["amount"] for p in found if p["made"]),
        "installments_due": sum(p["amount"] for p in found if not p["made"]),
    }


def charged_chart(snapshot: Snapshot, cards: list[str], month: str = "") -> dict:
    """
    What was charged to the credit cards (see charge_statements) in each statement month,
    by the closing dates of the cards, from the first month of the ledger to the last one
    (or to `month`), or to the last statement month with a charge.
    """
    rows = snapshot.all_rows
    if not len(rows):
        return {"charged_chart": None, "charged_series": []}
    totals: dict[str, list[float]] = {}  # statement month to its charges
    for _ix, statement, _card, amount, _n in charge_statements(snapshot, cards):
        if not month or statement <= month:
            totals.setdefault(statement, []).append(amount)
    first = min([min(rows.move_date)[:7], *totals])
    last = month or max([max(rows.move_date)[:7], *totals])
    series, cur = [], first
    while cur <= last:
        series.append({"month": cur, "value": sum(totals.get(cur, [])), "count": len(totals.get(cur, []))})
        cur = next_month(cur)
    return {"charged_chart": column_chart(series), "charged_series": series}


def installments(amount: float, n: int) -> list[float]:
    """An amount split into n installments, in cents: the last one with what the others leave."""
    each = round(amount / n, 2)
    return [*([each] * (n - 1)), round(amount - each * (n - 1), 2)]


def paid_chart(snapshot: Snapshot, cards: list[str], month: str = "") -> dict:
    """
    What is paid of the charges to the credit cards (see charge_statements) in each statement
    month: each charge split into its installments (see INSTALLMENTS_COLUMN), one a statement
    from its own on, so a charge in one installment is paid in its own statement. An
    installment is made once its statement has closed (before today), else it is still due.
    With `month`, only of the charges of the statements up to it. From the first month of the
    ledger to the last one with an installment.
    """
    rows = snapshot.all_rows
    if not len(rows):
        return {"paid_chart": None, "paid_series": [], "paid_due": 0.0}
    today = snapshot.today
    totals: dict[str, dict] = {}  # statement month to its installments made and due
    for _ix, statement, card, amount, n in charge_statements(snapshot, cards):
        if month and statement > month:
            continue
        for i, value in enumerate(installments(amount, n)):
            when = add_months(statement, i)
            total = totals.setdefault(when, {"made": 0.0, "due": 0.0, "count": 0})
            total["made" if card.closing_date(when) < today else "due"] += value
            total["count"] += 1
    first = min([min(rows.move_date)[:7], *totals])
    last = max([month or max(rows.move_date)[:7], *totals])
    series, cur = [], first
    while cur <= last:
        total = totals.get(cur, {"made": 0.0, "due": 0.0, "count": 0})
        series.append({"month": cur, "value": total["made"] + total["due"], **total})
        cur = next_month(cur)
    return {
        "paid_chart": column_chart(series),
        "paid_series": series,
        "paid_due": sum(p["due"] for p in series),
    }


def owed_charts(snapshot: Snapshot, cards: list[str], month: str = "") -> dict:
    """
    The net owed on the credit cards at each month end, up to `month`: in the base
    currency, nominal and real (deflated by the CPI at the end of each month, see
    with_real), and over equity.
    """
    monthly = month_end_balances(snapshot, month)
    owed = ratio_series(snapshot, monthly, lambda b, _total: sum(b.get(name, 0.0) for name in cards))
    amounts = []
    for p in owed:
        change = p["amount"] - amounts[-1]["value"] if amounts else p["amount"]
        amounts.append({"date": p["date"], "month": p["month"], "value": p["amount"], "change": change})
    real_date = with_real(
        amounts, snapshot.base_currency.customer_price_index, [month_end(p["month"]) for p in amounts]
    )
    owed_chart = line_chart(amounts)
    if owed_chart:
        owed_chart["real_date"] = real_date
    return {
        "owed_chart": owed_chart,
        "owed_amounts": amounts,
        "owed_ratio": line_chart(owed, percent),
        "owed_series": owed,
    }


# The buttons of the base movements of the Credit Cards tab: their label and color (orange
# for a payment, blue for a surplus), the payment first, picked by default.
CARD_BUTTONS = {CARD_PAYMENT: ("Payment", "liability"), CARD_SURPLUS: ("Surplus", "asset")}


def card_payment_fields(snapshot: Snapshot, base_movement: str = "") -> dict:
    """
    The base movements of the Credit Cards tab (CARD_PAYMENT_BASES) to pick from, as buttons
    (see CARD_BUTTONS), with the account fields of the selected one (the payment by
    default), what the others should be if they aren't as expected, and the cards in
    another currency, which need what was paid in their currency.
    """
    bases, missing = snapshot.tab_base_movements(CARD_PAYMENT_BASES, CREDIT_CARD)
    buttons = {n: b for n, b in CARD_BUTTONS.items() if n in bases}
    if base_movement not in buttons:
        base_movement = next(iter(buttons), "")
    return {
        "buttons": buttons,
        "base_movement": base_movement,
        "missing": missing,
        "fields": account_fields(snapshot, base_movement),
        "foreign_cards": {
            a.name: a.currency.name for a in snapshot.accounts.values()
            if isinstance(a, CreditCardAccount) and is_foreign(a)
        },
    }
