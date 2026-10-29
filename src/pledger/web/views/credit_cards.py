"""credit_cards.py: the Credit Cards tab: what is owed and charged, and the card payment form."""

from __future__ import annotations

from pledger.models import CREDIT_CARD, CreditCardAccount, next_month, rounds_to_zero
from pledger.web.store import CARD_PAYMENT, CARD_PAYMENT_BASES, CARD_SURPLUS, Snapshot
from pledger.web.views.charts import column_chart, line_chart, with_real
from pledger.web.views.common import (
    account_movements,
    composition,
    month_end,
    month_end_balances,
    months_of,
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
    was charged to them in each month, up to `month`.
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
        }
        for a in cards
    ]
    return {
        "accounts": accounts,
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
        **owed_charts(snapshot, [a.name for a in cards], month),
        **charged_chart(snapshot, [a.name for a in cards], month),
    }


def charged_chart(snapshot: Snapshot, cards: list[str], month: str = "") -> dict:
    """
    What was charged to the credit cards (their credits, leaving out the initial
    movements and the payments) in each month, from the first month of the ledger
    to the last one (or to `month`).
    """
    rows = snapshot.all_rows
    if not len(rows):
        return {"charged_chart": None, "charged_series": []}
    charges = rows[rows.acc_credit.isin(cards) & ~rows.base_movement.isin(snapshot.initial_base_names)]
    totals: dict[str, list[float]] = {}  # month to its charges
    for move in snapshot.ledger(charges).movements:
        totals.setdefault(str(move.move_date)[:7], []).append(move.amount)
    series, cur, last = [], min(rows.move_date)[:7], month or max(rows.move_date)[:7]
    while cur <= last:
        series.append({"month": cur, "value": sum(totals.get(cur, [])), "count": len(totals.get(cur, []))})
        cur = next_month(cur)
    return {"charged_chart": column_chart(series), "charged_series": series}


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
    default), and what the others should be if they aren't as expected.
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
    }
