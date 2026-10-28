"""views.py"""

from __future__ import annotations

import datetime as dt
import math
import re

from pandas import DataFrame, Period, Series, Timedelta, Timestamp

from pledger.enums import (
    AccountCheck,
    AccountType,
    CreditorKind,
    InvestmentType,
    LedgerSide,
    MovementType,
)
from pledger.ledger import Ledger
from pledger.models import (
    CPI_BASE,
    CREDIT_CARD,
    DEBT,
    INVESTMENT,
    SPECIAL_LABELS,
    CreditCardAccount,
    Currency,
    DebtAccount,
    InvestmentAccount,
    MovementBase,
    next_month,
    rounds_to_zero,
    special_of,
)
from pledger.web.store import (
    BASE_AMOUNT,
    CARD_PAYMENT,
    CARD_PAYMENT_BASES,
    DEBT_BASES,
    DEBT_INCURRED,
    DEBT_PAID,
    INITIAL_GAIN,
    INTEREST,
    INVESTMENT_COLUMN,
    LOAN_COLLECTED,
    LOAN_GIVEN,
    OTHER_AMOUNT,
    QUANTITY_COLUMN,
    QUANTITY_OPTIONAL,
    QUANTITY_REQUIRED,
    Snapshot,
    Work,
    conversion_fingerprint,
    exchange_base_movement,
    interest_fingerprint,
    is_capital,
    row_fingerprint,
    work_fingerprint,
)

ACCOUNT_TYPE_ORDER = list(AccountType)

# The tab where each kind of movement that can't be deleted from the Ledger tab is
# handled: its colored circle there and its tooltip. The circle links to its row in
# that tab (see source_link).
MOVEMENT_SOURCES = {
    "investments": ("🟢", "Added, edited and deleted from the Investments tab"),
    "currencies": ("🔵", "Added, edited and deleted from the Currencies tab"),
    "revaluation": ("🔵", "Generated from the values of the Currencies tab"),
    "work": ("🟠", "Generated each week from the salary of the Work tab"),
    "accounts": ("🟣", "Generated from the initial amount of the account, in the Accounts tab"),
    "corrections": ("🟣", "A balance correction, added from the Accounts tab; it can be deleted here"),
    "debt": ("🟡", "Added, edited and deleted from the Debt tab"),
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
        "investments": "/investments", "currencies": "/currencies",
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
    initials = set(snapshot.initial_rows.index)
    ledger_bases = snapshot.ledger_base_movements()
    debt_bases, card_bases = snapshot.debt_base_movements(), snapshot.card_payment_base_movements()
    corrections = snapshot.correction_base_movements()

    def source(ix, move) -> str | None:
        if move.move_base.name in invest:
            return "investments"
        if ix in conversions:
            return "currencies"
        if ix in revaluations:
            return "revaluation"
        if ix in salaries:
            return "work"
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


def build_view(
    snapshot: Snapshot, month: str = "", account: str = "", initial: bool = False, base: str = ""
) -> dict:
    """
    Filtered movements (by month, account and base movement) and results for the ledger
    view, without the initial ones by default.
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
        "movements": movements,
        # Not shown in the Ledger tab, but in /api/summary
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


EDITABLE_ACCOUNT_TYPES = {AccountType.ASSET, AccountType.LIABILITY}


def account_editing(account) -> dict:
    """
    What the account form needs to edit an account: its name and type, which stay, where
    its initial amount comes from if not from the form (its investments or creditors), and
    whether it is a credit card account, always liquid.
    """
    return {
        "name": account.name,
        "type": account.acc_type.value,
        "amount_from": (
            "investments" if isinstance(account, InvestmentAccount)
            else "creditors" if isinstance(account, DebtAccount)
            else None
        ),
        "credit_card": isinstance(account, CreditCardAccount),
    }


def account_values(account) -> dict:
    """The values of the account form for an account, to edit it."""
    return {
        "acc_type": account.acc_type.value,
        "acc_name": account.name,
        "acc_description": account.description,
        "acc_currency": account.currency.name,
        "acc_is_liquid": account.is_liquid,
        "acc_initial_amount": f"{account.initial_amount:,.2f}",
        "acc_initial_date": account.initial_date.isoformat() if account.initial_date else "",
        "acc_check": account.checks[0].value if account.checks else "",
    }


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


def build_accounts(snapshot: Snapshot, month: str = "", values: dict | None = None) -> dict:
    """
    Balances per account and per type, their composition and the month-end ratios
    over equity, plus the state of the new account form.
    """
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
                "special": "Credit card" if isinstance(a, CreditCardAccount) else SPECIAL_LABELS.get(special_of(a), "-"),
                "checks": [CHECK_LABELS[c] for c in a.checks],
                "balance": balances.get(a.name, 0.0),
                "editable": a.acc_type in EDITABLE_ACCOUNT_TYPES,
                # Investment accounts get their balance from their investments
                "correctable": a.acc_type in EDITABLE_ACCOUNT_TYPES and not isinstance(a, InvestmentAccount),
            }
            for a in snapshot.accounts.values()
            if a.acc_type == t
        ]
        for t in ACCOUNT_TYPE_ORDER
    }
    totals = {t: sum(a["balance"] for a in accs) for t, accs in accounts_by_type.items()}

    return {
        "months": months_of(snapshot),
        "month": month,
        "assets": totals[AccountType.ASSET.value],
        "liabilities": totals[AccountType.LIABILITY.value],
        "equity": totals[AccountType.EQUITY.value],
        "accounts_by_type": accounts_by_type,
        "groupings": balance_groupings(snapshot, balances),
        **over_time_charts(snapshot, month),
        **account_form(snapshot, values),
    }


def account_form(snapshot: Snapshot, values: dict | None = None, editing: dict | None = None,
                 correcting: dict | None = None) -> dict:
    """
    The state of the account form: of a new account, of an account `editing` (see
    account_editing) or of the correction of the balance of one, `correcting` (see
    account_correcting).
    """
    values = values or {}
    values.setdefault("acc_type", AccountType.ASSET.value)
    values.setdefault("acc_currency", snapshot.base_currency.name)
    values.setdefault("acc_initial_amount", "0.00")
    return {
        # New equity accounts are not supported for now.
        "account_types": [t.value for t in ACCOUNT_TYPE_ORDER if t != AccountType.EQUITY],
        "currencies": list(snapshot.currencies.values()),
        "check_labels": CHECK_LABELS,
        "values": values,
        "editing": editing,
        "correcting": correcting,
    }


def account_correcting(snapshot: Snapshot, account) -> dict:
    """What the account form needs to correct the balance of an account: its balance today, in its currency."""
    today = dt.date.today().isoformat()
    if account.currency.is_base_currency:
        rows = snapshot.all_rows
        balance = snapshot.ledger(rows[rows.move_date <= today]).balances().get(account.name, 0.0)
    else:
        balance = snapshot.own_balance(account.name, today)
    return {"name": account.name, "currency": account.currency.name, "balance": balance}


CHECK_LABELS = {
    AccountCheck.NET_POSITIVE: "Net positive",
    AccountCheck.NET_ZERO: "Net zero",
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


NO_TYPE = "No type"  # label of the investments only in the ledger, not in the investments YAML
BROKER_FEES = "Broker fees"  # the investment base movement of the fees, shown as a share of the amount


def is_closed(holding: dict) -> bool:
    """An investment no longer held (or never held): no quantity and no amount."""
    return holding["total_quantity"] == 0 and rounds_to_zero(holding["total_amount"])


def build_investments(snapshot: Snapshot) -> dict:
    """
    Investments held in each investment account, after the ledger movements, grouped
    by type and by account, with their results: the net capital gains (capital
    gains minus losses, whatever was bought or sold since), the totals of the other
    results (dividends, broker fees...) by base movement and their net total. The
    closed ones (see is_closed) are listed apart, and the ones with no type and the
    results naming an unknown investment are flagged.
    """
    unattributed: list[dict] = []
    accounts = snapshot.investment_accounts(unattributed)
    bases = snapshot.invest_base_movements()
    sign = result_signs(snapshot)
    capital = {n for n, b in bases.items() if is_capital(b)}
    # The other base movements with results, in their defined order, and whether they are losses.
    result_bases = {
        n: b.move_type == MovementType.LOSS
        for n, b in bases.items()
        if n not in capital and any(n in r for a in accounts for r in a.results.values())
    }

    def results_of(a: InvestmentAccount, name: str) -> dict:
        results = a.results.get(name, {})
        return {
            "capital": sum(sign[n] * v for n, v in results.items() if n in capital),
            "results": {n: v for n, v in results.items() if n not in capital},
            "total": sum(sign[n] * v for n, v in results.items()),
        }

    closed = sorted(
        (
            {
                "name": name,
                "account": a.name,
                "type": INVESTMENT_TYPE_LABELS.get(h["investment"].type, NO_TYPE),
                **results_of(a, name),
            }
            for a in accounts for name, h in a.investments.items() if is_closed(h)
        ),
        key=lambda c: (c["name"], c["account"]),
    )
    for a in accounts:
        a.investments = {n: h for n, h in a.investments.items() if not is_closed(h)}
    holdings = [
        {
            "account": a.name,
            "name": name,
            "type": INVESTMENT_TYPE_LABELS.get(h["investment"].type, NO_TYPE),
            "amount": h["total_amount"],
            "quantity": h["total_quantity"],
            # Amount per unit; none without a quantity.
            "price": h["total_amount"] / h["total_quantity"] if h["total_quantity"] else None,
            **results_of(a, name),
        }
        for a in accounts
        for name, h in a.investments.items()
    ]
    total = sum(h["amount"] for h in holdings)
    for h in holdings:
        h["share"] = round(100 * h["amount"] / total, 1) + 0.0 if total else 0.0
    held = {
        "amount": total,
        "capital": sum(h["capital"] for h in holdings),
        "results": {n: sum(h["results"].get(n, 0.0) for h in holdings) for n in result_bases},
        "total": sum(h["total"] for h in holdings),
    }
    for i in [*holdings, held]:
        i["fees_share"] = fees_share(i)

    # Types and accounts keep their defined order, listing only the ones with
    # investments. Investment names are only unique within an account.
    by_type = {
        t: {f"{h['name']} ({h['account']})": h["amount"] for h in holdings if h["type"] == t}
        for t in [*INVESTMENT_TYPE_LABELS.values(), NO_TYPE]
        if any(h["type"] == t for h in holdings)
    }
    by_account = {
        a.name: {name: h["total_amount"] for name, h in a.investments.items()}
        for a in accounts
        if a.investments
    }
    interest = build_interest(snapshot)
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
        "held": held,  # the totals of the holdings
        "has_fees": BROKER_FEES in result_bases,
        "closed": closed,
        "result_bases": result_bases,
        "has_capital": bool(capital),
        "has_results": bool(capital or result_bases),
        # Of the held and closed investments, and the interest on money
        "result_totals": {
            "capital": sum(i["capital"] for i in [*holdings, *closed]),
            **{n: sum(i["results"].get(n, 0.0) for i in [*holdings, *closed]) for n in result_bases},
            "interest": interest["interest_totals"]["net"],
            "total": sum(i["total"] for i in [*holdings, *closed]) + interest["interest_totals"]["net"],
        },
        **interest,
        "unattributed": unattributed,
        "untyped": [h for h in holdings if h["type"] == NO_TYPE],
        "total": total,
        "groupings": {
            "type": grouped_composition(by_type),
            "account": grouped_composition(by_account),
        },
        **investment_charts(snapshot),
    }


def fees_share(results: dict) -> float | None:
    """
    The broker fees of an investment (or of several) as a share of its amount, or
    None without fees or a positive amount.
    """
    fees = results["results"].get(BROKER_FEES, 0.0)
    return fees / results["amount"] if fees and results["amount"] > 0 else None


def interest_movements(snapshot: Snapshot) -> list:
    """
    The movements of interest on money, of the interest CSV: the interest earned or
    paid on the money of a regular account.
    """
    return snapshot.ledger(snapshot.interest_rows).movements


def build_interest(snapshot: Snapshot) -> dict:
    """
    The interest on money (see interest_movements) earned and paid in each account,
    in their defined order, and its net in each month, from the first month of the
    ledger to the last one. `has_interest` if its base movements are defined.
    """
    moves = interest_movements(snapshot)
    by_account: dict[str, dict] = {}
    for a in snapshot.accounts.values():
        # Each movement counts for its account on the side that isn't equity.
        mine = [m for m in moves if a in (m.acc_debit, m.acc_credit)]
        if mine and a.acc_type != AccountType.EQUITY:
            gains = sum(m.amount for m in mine if m.move_type == MovementType.GAIN)
            losses = sum(m.amount for m in mine if m.move_type == MovementType.LOSS)
            by_account[a.name] = {
                "name": a.name, "description": a.description, "currency": a.currency.name,
                "gains": gains, "losses": losses, "net": gains - losses, "count": len(mine),
            }
    totals: dict[str, float] = {}  # month to its net interest
    for m in moves:
        sign = 1 if m.move_type == MovementType.GAIN else -1
        month = str(m.move_date)[:7]
        totals[month] = totals.get(month, 0.0) + sign * m.amount
    series = []
    dates = snapshot.all_rows.move_date
    if moves and len(dates):
        cur, last = min(dates)[:7], max(dates)[:7]
        while cur <= last:
            series.append({"month": cur, "value": totals.get(cur, 0.0)})
            cur = next_month(cur)
    accounts = list(by_account.values())
    return {
        "has_interest": bool(INTEREST & snapshot.base_movements.keys()),
        "interest_accounts": accounts,
        "interest_totals": {k: sum(a[k] for a in accounts) for k in ("gains", "losses", "net")},
        "interest_chart": column_chart(series) if series else None,
        "interest_series": series,
    }


def result_signs(snapshot: Snapshot) -> dict[str, int]:
    """The sign each investment base movement adds to a result with: -1 for losses."""
    return {
        n: -1 if b.move_type == MovementType.LOSS else 1
        for n, b in snapshot.invest_base_movements().items()
    }


def investment_charts(snapshot: Snapshot) -> dict:
    """
    The total invested (the amounts of all investments) and the total result of
    the investments, held or not, and of the interest on money, at the end of each
    month of the ledger. The total invested also gets its real value (see with_real).
    """
    dates = snapshot.all_rows.move_date
    if not len(dates):
        return {"invested_chart": None, "invested_series": [], "result_chart": None, "result_series": []}
    sign = result_signs(snapshot)
    interest = [
        (str(m.move_date)[:7], m.amount if m.move_type == MovementType.GAIN else -m.amount)
        for m in interest_movements(snapshot)
    ]
    invested, result = [], []
    month, last = min(dates)[:7], max(dates)[:7]
    while month <= last:
        accounts = snapshot.investment_accounts(month=month)
        for series, value in (
            (invested, sum(h["total_amount"] for a in accounts for h in a.investments.values())),
            (result, sum(sign[n] * v for a in accounts for r in a.results.values() for n, v in r.items())
                + sum(v for m, v in interest if m <= month)),
        ):
            change = value - series[-1]["value"] if series else value
            series.append({"date": f"{month}-01", "month": month, "value": value, "change": change})
        month = next_month(month)
    invested_real = with_real(
        invested, snapshot.base_currency.customer_price_index, [month_end(p["month"]) for p in invested]
    )
    invested_chart = line_chart(invested)
    if invested_chart:
        invested_chart["real_date"] = invested_real
    return {
        "invested_chart": invested_chart,
        "invested_series": invested,
        "result_chart": line_chart(result),
        "result_series": result,
    }


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


def build_invest_movements(snapshot: Snapshot) -> list[dict]:
    """
    The movements of the investment base movements, newest first: those of the
    investments CSV, with their investment and quantity, and those of the interest
    CSV. Their `index` is their row in the CSV of their `source`. Their `account` is the
    investment account on a side, else, like for their results (e.g. broker fees),
    the first one with an investment of their name, if any. Each one has the total amount
    of its investment account, and the amount and quantity of its investment, right after
    it, all the movements counted (see _apply_investments), or None without them.
    """
    holders = [a for a in snapshot.investment_accounts() if a.investments]
    movements = []
    for source, rows, generated, fingerprint in (
        ("investments", snapshot.investments, snapshot.investment_rows, row_fingerprint),
        ("interest", snapshot.interest, snapshot.interest_rows, interest_fingerprint),
    ):
        # movement_rows lists them newest first: back to the order of the CSV
        moves = movement_rows(snapshot, generated, snapshot.ledger(generated))[::-1]
        for i, (m, (_, row)) in enumerate(zip(moves, rows.iterrows())):
            investment = row.get(INVESTMENT_COLUMN, "")
            account = next(
                (n for n in (m["debit"], m["credit"]) if isinstance(snapshot.accounts[n], InvestmentAccount)),
                next((a.name for a in holders if investment and investment in a.investments), None),
            )
            movements.append({
                **m, "index": i, "fingerprint": fingerprint(row), "source": source, "editable": True,
                "account": account,  # the investment account, if any
                "investment": investment, "quantity": row.get(QUANTITY_COLUMN, ""),
            })
    movements.sort(key=lambda m: m["date"])  # stable: in their order on each date
    # From the initial amounts and quantities of the investments file
    totals = {a.name: sum(h["total_amount"] for h in a.investments.values())
              for a in snapshot.accounts.values() if isinstance(a, InvestmentAccount)}
    held = {(a.name, n): [h["total_amount"], h["total_quantity"]]
            for a in snapshot.accounts.values() if isinstance(a, InvestmentAccount) for n, h in a.investments.items()}
    for m in movements:
        units = float(str(m["quantity"]).replace(",", "") or 0)
        for name, sign in ((m["debit"], 1), (m["credit"], -1)):
            if name in totals and m["investment"]:
                totals[name] += sign * m["amount"]
                holding = held.setdefault((name, m["investment"]), [0.0, 0.0])
                holding[0] += sign * m["amount"]
                holding[1] += sign * units
        holding = held.get((m["account"], m["investment"])) if m["investment"] else None
        m["account_total"] = totals.get(m["account"])
        m["investment_amount"], m["investment_quantity"] = holding if holding else (None, None)
    movements.reverse()
    return movements


def invest_filter(movements: list[dict], account: str = "", investment: str = "", base: str = "") -> dict:
    """
    The investment movements of an investment `account`, of an `investment` and of a
    `base` movement (all of them without any), whatever accounts are their debit and
    credit ones, with the accounts, the investments (of the account picked, if any) and
    the base movements to pick from. An investment not of the account picked is dropped.
    """
    by_account: dict[str, set[str]] = {}
    for m in movements:
        if m["account"]:
            by_account.setdefault(m["account"], set())
            if m["investment"]:
                by_account[m["account"]].add(m["investment"])
    account = account if account in by_account else ""
    names = by_account[account] if account else {m["investment"] for m in movements if m["investment"]}
    investment = investment if investment in names else ""
    bases = sorted({m["base"] for m in movements})
    base = base if base in bases else ""
    return {
        "movements": [
            m for m in movements
            if (not account or m["account"] == account) and (not investment or m["investment"] == investment)
            and (not base or m["base"] == base)
        ],
        "invest_bases": bases,
        "invest_base": base,
        "invest_accounts": sorted(by_account),
        "invest_names": sorted(names),
        "invest_account": account,
        "investment": investment,
    }


def build_conversions(snapshot: Snapshot, currency: str = "", account: str = "") -> list[dict]:
    """
    The rows of the conversions CSV, newest first, with the amount in each currency,
    the value of the other currency in the base one they imply and the balance of the
    account in the other currency right after it, in that currency; with a `currency`,
    only those from or to it, and with an `account`, only those of that account.
    """
    after = snapshot.conversion_balances()
    conversions = []
    ids = [snapshot.ids[ix] for ix in snapshot.conversion_rows.index]  # in the order of the CSV
    for i, (ix, row) in enumerate(snapshot.conversions.iterrows()):
        base_amount = float(row[BASE_AMOUNT].replace(",", "") or 0)
        other_amount = float(row[OTHER_AMOUNT].replace(",", "") or 0)
        debit, credit = snapshot.accounts[row["acc_debit"]], snapshot.accounts[row["acc_credit"]]
        other = debit if not debit.currency.is_base_currency else credit
        conversions.append({
            "index": i,
            "id": ids[i],
            "fingerprint": conversion_fingerprint(row),
            "date": row["move_date"],
            "base": row["base_movement"],
            "debit": debit.name,
            "credit": credit.name,
            "amount": base_amount,
            "other_amount": other_amount,
            "currency": other.currency.name,
            "account": other.name,
            "other_balance": after.get(i),  # None if the currency has no values
            "bought": other is debit,  # the other currency comes in, else goes out
            "rate": base_amount / other_amount if other_amount else None,
            "comment": row.get("comment", ""),
        })
    conversions.sort(key=lambda c: c["date"])  # stable: in their order on each date
    conversions.reverse()
    return [
        c for c in conversions
        if (not currency or c["currency"] == currency) and (not account or c["account"] == account)
    ]


def conversion_filter(snapshot: Snapshot, currency: str = "", account: str = "") -> dict:
    """
    The conversions of the currency and the account picked in the filter (all without
    them), with the currencies to pick from and the accounts in another currency (of
    the currency picked, if any). An account not of the currency picked is dropped.
    """
    others = [c for c in snapshot.currencies if c != snapshot.base_currency.name]
    currency = currency if currency in others else ""
    accounts = [
        a.name for a in snapshot.accounts.values()
        if a.currency.name in others and a.acc_type != AccountType.EQUITY and (not currency or a.currency.name == currency)
    ]
    account = account if account in accounts else ""
    return {
        "conversions": build_conversions(snapshot, currency, account),
        "conversion_currencies": others,
        "conversion_currency": currency,
        "conversion_accounts": accounts,
        "conversion_account": account,
    }


def conversion_fields(snapshot: Snapshot, sold: str = "", bought: str = "", changed: str = "") -> dict:
    """
    The currencies sold and bought of a new conversion, one of them the base one,
    and the asset accounts it can be from (in the sold currency) and to (in the
    bought one), or the fixed ones of its base movement (see exchange_base_movement). By
    default, the currencies of the last conversion, else selling the base currency
    for the first other one. When they clash (both or neither the base currency),
    the one not `changed` ("sold" or "bought") gives way.
    """
    names = list(snapshot.currencies)
    base = snapshot.base_currency.name
    others = [n for n in names if n != base]
    if sold not in names and bought not in names and len(snapshot.conversions):
        last = snapshot.conversions.iloc[-1]
        debit, credit = snapshot.accounts.get(last["acc_debit"]), snapshot.accounts.get(last["acc_credit"])
        if debit is not None and credit is not None:
            sold, bought = credit.currency.name, debit.currency.name
    if sold not in names:
        sold = base if bought != base else next(iter(others), base)
    if bought not in names:
        bought = next(iter(others), base) if sold == base else base
    if (sold == base) == (bought == base) and others:
        if changed == "bought":
            sold = base if bought != base else others[0]
        else:
            bought = base if sold != base else others[0]
    try:
        base_movement, missing = exchange_base_movement(snapshot, sold, bought), None
    except ValueError as e:
        base_movement, missing = "", str(e)
    move_base = snapshot.base_movements.get(base_movement)

    def options(currency: str) -> list[str]:
        """The asset accounts in that currency, but the investment ones."""
        return [
            a.name for a in snapshot.accounts.values()
            if a.acc_type == AccountType.ASSET and a.currency.name == currency and not isinstance(a, InvestmentAccount)
        ]

    return {
        "currencies": names,
        "sold": sold,
        "bought": bought,
        "base_movement": base_movement,
        "missing": missing,
        "fields": {
            "from_fixed": move_base.acc_credit.name if move_base and move_base.acc_credit else None,
            "to_fixed": move_base.acc_debit.name if move_base and move_base.acc_debit else None,
            "from_options": options(sold),
            "to_options": options(bought),
        },
    }


def invest_fields(snapshot: Snapshot, base_movement: str = "", move_type: str = "") -> dict:
    """
    The investment base movements to pick from for a movement type (without one,
    that of the base movement, else a loss: see default_move_type), but the
    conversions, and the fields of the selected one (the first one by default): its
    accounts, whether it names an investment ("required" with an INVESTMENT side,
    else "optional") and has a quantity ("required", "optional" or None), plus the
    known investments to pick from.
    """
    bases = {
        n: b for n, b in snapshot.invest_base_movements().items() if n not in snapshot.conversion_base_names
    }
    by_type = {t: [n for n in bases if bases[n].move_type.value == t] for t in bases_by_type(bases)}
    if move_type not in by_type:
        move_type = bases[base_movement].move_type.value if base_movement in bases else default_move_type(by_type)
    if base_movement not in by_type[move_type]:
        base_movement = next(iter(by_type[move_type]), "")
    base = bases.get(base_movement)
    side = base is not None and INVESTMENT in {base.debit_special, base.credit_special}
    return {
        "move_types": list(by_type),
        "move_type": move_type,
        "bases": by_type[move_type],
        "base_movement": base_movement,
        "fields": account_fields(snapshot, base_movement),
        "investment": None if base_movement in INTEREST else "required" if side else "optional",
        "quantity": (
            "required" if base_movement in QUANTITY_REQUIRED
            else "optional" if base_movement in QUANTITY_OPTIONAL
            else None
        ),
        # Those of the investments YAML and those only in the ledger, by account
        "investments_by_account": {
            a.name: sorted(a.investments) for a in snapshot.investment_accounts() if a.investments
        },
    }


def new_investment_fields(snapshot: Snapshot) -> dict:
    """The investment accounts and investment types to pick from for a new investment."""
    return {
        "accounts": [a.name for a in snapshot.accounts.values() if isinstance(a, InvestmentAccount)],
        "types": INVESTMENT_TYPE_LABELS,
    }


CREDITOR_KIND_LABELS = {
    CreditorKind.PERSON: "People", CreditorKind.SERVICE: "Services", CreditorKind.COMPANY: "Companies",
}
CREDITOR_KIND_EMOJIS = {CreditorKind.PERSON: "👥", CreditorKind.SERVICE: "🧾", CreditorKind.COMPANY: "🏢"}


UNASSIGNED = "-"  # the who filter of the Debt tab for the movements naming no one


def build_debts(snapshot: Snapshot, month: str = "", who: str = "") -> dict:
    """
    The people, services and companies in each debt account: the ones we owe
    (creditors) and the ones that owe us (debtors), with what we owe and are owed
    as positive amounts, plus the debt of the movements whose comment names no
    one (unassigned), as of the end of `month` (all the ledger by default). Lists
    who isn't in their expected role, and the unassigned debt of each account by comment.
    """
    accounts = snapshot.debt_accounts(month)

    def label(a: DebtAccount, name: str) -> str:
        # Names are only unique within an account.
        return f"{name} ({a.name})" if len(accounts) > 1 else name

    def by_kind(people_of, sign: int) -> list[dict]:
        return grouped_composition({
            title: {label(a, n): sign * c.amount for a in accounts for n, c in people_of(a).items() if c.kind == kind}
            for kind, title in CREDITOR_KIND_LABELS.items()
            if any(c.kind == kind for a in accounts for c in people_of(a).values())
        })

    kinds = []
    for kind, title in CREDITOR_KIND_LABELS.items():
        rows = [
            {
                "account": a.name,
                "name": c.name,
                "role": "Creditor" if c.is_creditor else "Debtor" if c.is_debtor else "Settled",
                "expected": c.expected.value.capitalize() if c.expected else None,
                "met": c.meets_expected,
                "amount": c.amount,
            }
            for a in accounts
            for c in a.get_kind(kind).values()
        ]
        kinds.append({
            "kind": kind.value, "title": title, "emoji": CREDITOR_KIND_EMOJIS[kind],
            "rows": rows, "net": sum(r["amount"] for r in rows),
        })

    # Each movement is about whoever its comment names, in its debt account.
    movements = account_movements(
        snapshot, [a.name for a in accounts], month, editable=snapshot.debt_base_movements()
    )
    for m in movements:
        debt = next(a for a in accounts if a.name in (m["debit"], m["credit"]))
        m["account"] = debt.name
        m["who"] = debt.creditor_in(m["comment"])
    after = debt_after(snapshot, accounts)
    for m in movements:
        m["debt"] = after.get(m["index"])
    # Who to pick in the filter of the movements, by kind
    who_options = {
        title: sorted({n for a in accounts for n, c in a.creditors.items() if c.kind == kind})
        for kind, title in CREDITOR_KIND_LABELS.items()
    }
    names = {n for ns in who_options.values() for n in ns}
    if who not in names and who != UNASSIGNED:
        who = ""
    if who:
        movements = [m for m in movements if (m["who"] or UNASSIGNED) == who]

    we_owe = sum(c.amount for a in accounts for c in a.get_creditors().values())
    owed_to_us = -sum(c.amount for a in accounts for c in a.get_debtors().values())
    unassigned = sum(a.unassigned for a in accounts)
    return {
        "accounts": [
            {
                "name": a.name,
                "description": a.description,
                "currency": a.currency.name,
                "counts": {title: len(a.get_kind(kind)) for kind, title in CREDITOR_KIND_LABELS.items()},
                "unassigned": a.unassigned,
                "net": sum(c.amount for c in a.creditors.values()) + a.unassigned,
            }
            for a in accounts
        ],
        "kinds": kinds,
        "months": months_of(snapshot),
        "month": month,
        "unmet": [
            {**r, "kind": k["title"]} for k in kinds for r in k["rows"] if not r["met"]
        ],
        "unassigned_accounts": [
            {
                "name": a.name,
                "amount": a.unassigned,
                "comments": sorted(
                    ((c or "(no comment)", v) for c, v in a.unassigned_comments.items()
                     if not rounds_to_zero(v)),
                    key=lambda cv: -abs(cv[1]),
                ),
            }
            for a in accounts
            if not rounds_to_zero(a.unassigned)
        ],
        "multiple_accounts": len(accounts) > 1,
        "movements": movements,
        "who": who,
        "who_options": {title: ns for title, ns in who_options.items() if ns},
        "we_owe": we_owe,
        "owed_to_us": owed_to_us,
        "unassigned": unassigned,
        "has_unassigned": not rounds_to_zero(unassigned),
        "net": we_owe - owed_to_us + unassigned,
        "creditors": by_kind(DebtAccount.get_creditors, 1),
        "debtors": by_kind(DebtAccount.get_debtors, -1),
    }


def debt_after(snapshot: Snapshot, accounts: list[DebtAccount]) -> dict[int, float]:
    """
    What we owe whoever each movement of these debt accounts is about (negative: what
    they owe us), or the unassigned debt of its account if it names no one, right after
    it, by its index in all_rows: from their initial amounts in the creditors YAML,
    through all the ledger, like _apply_debts. The initial movements are left out.
    """
    initial = {a.name: a for a in snapshot.accounts.values() if isinstance(a, DebtAccount)}
    debts: dict[tuple[str, str | None], float] = {
        (a.name, n): c.amount for a in initial.values() for n, c in a.creditors.items()
    }
    after = {}
    for m in account_movements(snapshot, [a.name for a in accounts])[::-1]:
        if m["initial"]:
            continue
        for name, sign in ((m["debit"], -1), (m["credit"], 1)):
            if name in initial:
                key = (name, initial[name].creditor_in(m["comment"]))
                debts[key] = debts.get(key, 0.0) + sign * m["amount"]
                after[m["index"]] = debts[key]
    return after


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
                "deductions": {n: f"{d:,.2f}" for n, d in m.deductions.items()},
                "payment_date": m.payment_date.isoformat() if m.payment_date else "",
            }
    return {
        "fields": {"month": month, **values},
        "deductions": work.deductions,
        "autofill": last_month_fields(work),
        "editing": month in by_month,
        "company_names": list(work.companies),
        "position_names": sorted({p for c in work.companies.values() for p in c.positions}),
    }


def last_month_fields(work: Work) -> dict | None:
    """
    The values of the fields of the work month form for the last month, to fill it
    in with them in the browser: all but its month and its received payment date.
    None without months.
    """
    if not work.months:
        return None
    m = work.months[-1]
    return {
        "month": m.month,
        "fields": {
            "company": m.company or "",
            "position": m.position or "",
            "gross": "" if m.gross is None else f"{m.gross:,.2f}",
            **{f"deduction:{n}": f"{m.deductions[n]:,.2f}" if n in m.deductions else "" for n in work.deductions},
        },
    }


def build_work(work: Work, form: dict | None = None, cpi: Series | None = None) -> dict:
    """
    The companies worked for and their job positions, every month of the work
    history (the newest first) and the totals of this year, from the gross salary
    to the net one through each deduction (the largest first), plus the form state.
    Worked months already over with no received payment date are flagged as unpaid.
    With the `cpi` of the base currency, the salary charts also get their real values.
    """
    today = dt.date.today()
    year, this_month = str(today.year), today.strftime("%Y-%m")
    this_year = [m for m in work.months if m.month[:4] == year and not m.is_gap]
    gross, net = sum(m.gross for m in this_year), sum(m.net for m in this_year)
    deducted = {n: sum(m.deductions.get(n, 0.0) for m in this_year) for n in work.deductions}
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
        "gross": gross,
        "deducted": gross - net,
        "net": net,
        "deductions": [
            {"name": n, "amount": a, "share": a / gross if gross else 0.0}
            for n, a in sorted(deducted.items(), key=lambda d: -d[1]) if a
        ],
        "form": form or work_form(work),
        **salary_charts(work, cpi),
    }


def salary_charts(work: Work, cpi: Series | None = None) -> dict:
    """
    The gross and net salary of every month, with the months without a job at 0,
    and their real values at the CPI of the end of each month (see with_real).
    """
    def chart(key: str) -> dict | None:
        series = [
            {"date": f"{m.month}-01", "month": m, "value": getattr(m, key) or 0.0}
            for m in work.months
        ]
        real_date = with_real(series, cpi, [month_end(m.month) for m in work.months])
        drawn = line_chart(series)
        if drawn:
            drawn["real_date"] = real_date
        return drawn

    return {
        "gross_chart": chart("gross"),
        "net_chart": chart("net"),
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


def bases_by_type(bases: dict[str, MovementBase]) -> dict[str, list[str]]:
    """Names of these base movements, sorted and grouped by movement type (every type, even without any)."""
    return {
        t.value: [n for n in sorted(bases) if bases[n].move_type == t]
        for t in MovementType
    }


def default_move_type(by_type: dict[str, list[str]]) -> str:
    """The movement type picked by default: a loss, unless there are only base movements of other types."""
    if by_type.get(MovementType.LOSS.value):
        return MovementType.LOSS.value
    return next((t for t, names in by_type.items() if names), MovementType.LOSS.value)


def base_fields(snapshot: Snapshot, move_type: str = "", base_movement: str = "") -> dict:
    """
    Base movements of the Ledger tab to pick from for a movement type, and the
    account fields of the selected one. Without a type, it is taken from the base
    movement or defaults to a loss (see default_move_type).
    """
    ledger_bases = snapshot.ledger_base_movements()
    by_type = bases_by_type(ledger_bases)
    base = ledger_bases.get(base_movement)
    if move_type not in by_type:
        move_type = base.move_type.value if base else default_move_type(by_type)
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


# The base movements of the Debt tab, in two kinds: the debt we owe and the loans we give,
# with the color of their side and the label of the button of each base movement.
DEBT_PAIRS = [
    ("Debt", "liability", {DEBT_INCURRED: "Incurred", DEBT_PAID: "Paid"}),
    ("Loan", "asset", {LOAN_GIVEN: "Given", LOAN_COLLECTED: "Collected"}),
]


def debt_form_fields(snapshot: Snapshot, base_movement: str = "", kind: str = "") -> dict:
    """
    The debt base movements to pick from in the Debt tab: first the kind (see DEBT_PAIRS),
    that of the base movement if any, else `kind`, else the first one; then the base
    movements of that kind, with the account fields of the selected one (the first one by
    default). Also the ones of DEBT_BASES that aren't as expected, and who of the debt
    accounts a movement can be about, by kind: people and services, but no companies, whose
    debt comes from the salaries of the Work tab.
    """
    bases, missing = snapshot.tab_base_movements(DEBT_BASES, DEBT)
    kinds = [
        {
            "title": title, "color": color,
            # The second one (paid or collected) in a lighter color
            "buttons": {
                n: (label, color if i == 0 else f"{color}-light")
                for i, (n, label) in enumerate(labels.items()) if n in bases
            },
        }
        for title, color, labels in DEBT_PAIRS
        if any(n in bases for n in labels)
    ]
    picked = (
        next((k for k in kinds if base_movement in k["buttons"]), None)
        or next((k for k in kinds if k["title"] == kind), None)
        or next(iter(kinds), None)
    )
    buttons = picked["buttons"] if picked else {}
    if base_movement not in buttons:
        base_movement = next(iter(buttons), "")
    who: dict[str, set[str]] = {label: set() for label in CREDITOR_KIND_LABELS.values()}
    for a in snapshot.accounts.values():
        if isinstance(a, DebtAccount):
            for c in a.creditors.values():
                if c.kind != CreditorKind.COMPANY:
                    who[CREDITOR_KIND_LABELS[c.kind]].add(c.name)
    return {
        "kinds": [{"title": k["title"], "color": k["color"]} for k in kinds],
        "kind": picked["title"] if picked else "",
        "buttons": buttons,
        "bases": list(bases),
        "base_movement": base_movement,
        "missing": missing,
        "fields": account_fields(snapshot, base_movement),
        "who": {label: sorted(names) for label, names in who.items() if names},
    }


def card_payment_fields(snapshot: Snapshot) -> dict:
    """
    The card payment base movement of the Credit Cards tab (CARD_PAYMENT) with its account
    fields, or what it should be if it isn't as expected.
    """
    bases, missing = snapshot.tab_base_movements(CARD_PAYMENT_BASES, CREDIT_CARD)
    return {
        "base_movement": CARD_PAYMENT if bases else "",
        "missing": missing,
        "fields": account_fields(snapshot, CARD_PAYMENT),
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


def line_chart(series: list[dict], tick_label=compact) -> dict | None:
    """
    Points, path and axes to draw a line of {date, value} points (a None value
    breaks the line). Coordinates are percentages of the plot area, so HTML
    labels and the SVG line share them. Points with a `real` value (see with_real)
    also get a second, dashed line of those, on the same axes.
    """
    shown = [p for p in series if p["value"] is not None]
    if not shown:
        return None
    reals = [p["real"] for p in shown if p.get("real") is not None]
    values = [p["value"] for p in shown] + reals
    ticks = nice_ticks(min(values), max(values))
    y_lo, y_hi = ticks[0], ticks[-1]

    def y(v: float) -> float:
        return round(100 * (1 - (v - y_lo) / (y_hi - y_lo)), 2)

    x, x_ticks = time_axis([dt.date.fromisoformat(p["date"]) for p in series])
    points, path, joined = [], [], False
    real_path, real_joined, real_last = [], False, None
    for p in series:
        if p["value"] is None:
            joined = real_joined = False
            continue
        point = {**p, "x": x(p["date"]), "y": y(p["value"])}
        points.append(point)
        path.append(f"{'L' if joined else 'M'}{point['x']},{point['y']}")
        joined = True
        if p.get("real") is None:
            real_joined = False
            continue
        real_last = {"x": point["x"], "y": y(p["real"])}
        real_path.append(f"{'L' if real_joined else 'M'}{real_last['x']},{real_last['y']}")
        real_joined = True
    return {
        "path": " ".join(path),
        "points": points,
        "y_ticks": [{"y": y(t), "label": tick_label(t)} for t in ticks],
        "x_ticks": x_ticks,
        "last": points[-1],
        "real_path": " ".join(real_path) if reals else None,
        "real_last": real_last,
    }


def month_end(month: str) -> str:
    """The ISO date of the last day of a YYYY-MM month."""
    return Period(month, freq="M").end_time.date().isoformat()


def with_real(series: list[dict], cpi: Series | None, dates: list[str] | None = None) -> str | None:
    """
    Add to each {date, value} point its `real` value: deflated by the CPI of its
    date (or of its date in `dates`), in the base currency of the last of those
    dates with a CPI, which is returned (see real_values). None where there is no
    CPI, and everywhere without one.
    """
    for p in series:
        p["real"] = None
    if cpi is None or not len(cpi) or not series:
        return None
    index = [Timestamp(d) for d in (dates or [p["date"] for p in series])]
    values = Series([p["value"] if p["value"] is not None else math.nan for p in series], index=index)
    real, real_date = real_values(values, cpi)
    for p, r in zip(series, real):
        p["real"] = None if math.isnan(r) else float(r)
    return real_date if any(p["real"] is not None for p in series) else None


def column_chart(series: list[dict], tick_label=compact) -> dict | None:
    """
    Columns and axes to draw a {month, value} series, one band per month, up from
    0 (or down, for a negative value). Coordinates are percentages of the plot
    area. None if every value is 0.
    """
    lo = min(0.0, *(p["value"] for p in series))
    hi = max(0.0, *(p["value"] for p in series))
    if hi - lo < 1e-9:
        return None
    ticks = nice_ticks(lo, hi)

    def y(v: float) -> float:
        return round(100 * (1 - (v - ticks[0]) / (ticks[-1] - ticks[0])), 2)

    band = 100 / len(series)
    every = math.ceil(len(series) / 8)
    columns = [
        {
            **p,
            "x": round(band * (i + 0.5), 2),
            "y": y(p["value"]),
            "top": y(max(p["value"], 0.0)),
            "height": round(abs(y(p["value"]) - y(0.0)), 2),
            "negative": p["value"] < 0,
        }
        for i, p in enumerate(series)
    ]
    return {
        "columns": columns,
        "width": round(band * 0.7, 2),  # the rest of the band is the gap between columns
        "y_ticks": [{"y": y(t), "label": tick_label(t), "axis": abs(t) < 1e-9} for t in ticks],
        "x_ticks": [
            {"x": c["x"], "label": dt.date.fromisoformat(f"{c['month']}-01").strftime("%b %Y")}
            for c in columns[::every]
        ],
        "last": columns[-1],
    }


def waterfall_chart(statement: list[dict], tick_label=compact) -> dict | None:
    """
    Floating columns to draw a result statement as a waterfall: the gains of each
    result type up from the running total, then its losses down from there, in
    statement order, and the profit from 0. Coordinates are percentages of the
    plot area. None if every gain and loss is 0.
    """
    steps, running = [], 0.0
    for r in statement:
        for kind, value in (("gain", r["gains"]), ("loss", r["losses"])):
            steps.append({
                "name": r["name"], "short": r["short"], "kind": kind,
                "value": value, "start": running, "end": running + value,
            })
            running += value
    if not any(abs(s["value"]) > 1e-9 for s in steps):
        return None
    steps.append({"name": "Profit", "short": "Profit", "kind": "total", "value": running, "start": 0.0, "end": running})
    ticks = nice_ticks(min(0.0, *(s["end"] for s in steps)), max(0.0, *(s["end"] for s in steps)))

    def y(v: float) -> float:
        return round(100 * (1 - (v - ticks[0]) / (ticks[-1] - ticks[0])), 2)

    band = 100 / len(steps)
    columns = [
        {
            **s,
            "x": round(band * (i + 0.5), 2),
            "y": y(s["end"]),
            "top": y(max(s["start"], s["end"])),
            "height": round(abs(y(s["start"]) - y(s["end"])), 2),
        }
        for i, s in enumerate(steps)
    ]
    return {
        "columns": columns,
        "width": round(band * 0.7, 2),  # the rest of the band is the gap between columns
        "y_ticks": [{"y": y(t), "label": tick_label(t), "axis": abs(t) < 1e-9} for t in ticks],
        # Each result type labelled under its pair of columns, and the profit under its own
        "x_ticks": [
            {"x": round((a["x"] + b["x"]) / 2, 2), "label": a["short"]}
            for a, b in zip(columns[:-1:2], columns[1:-1:2])
        ] + [{"x": columns[-1]["x"], "label": "Profit"}],
        "last": columns[-1],
    }


def multi_line_chart(lines: list[dict], value_text=lambda v: f"{v:,.2f}") -> dict | None:
    """
    Paths and axes to draw several lines of {date, value} points (a None value
    breaks a line) on one y axis. `lines` are {name, series, ...} dicts, returned
    with their path and last point. The tooltip of each date lists the lines with a value on it.
    """
    shown = [p["value"] for line in lines for p in line["series"] if p["value"] is not None]
    if not shown:
        return None
    ticks = nice_ticks(min(shown), max(shown))

    def y(v: float) -> float:
        return round(100 * (1 - (v - ticks[0]) / (ticks[-1] - ticks[0])), 2)

    dates = sorted({p["date"] for line in lines for p in line["series"]})
    x, x_ticks = time_axis([dt.date.fromisoformat(d) for d in dates])
    drawn, values = [], {}
    for line in lines:
        path, joined, last = [], False, None
        for p in line["series"]:
            if p["value"] is None:
                joined = False
                continue
            values[line["name"], p["date"]] = p["value"]
            last = {"x": x(p["date"]), "y": y(p["value"])}
            path.append(f"{'L' if joined else 'M'}{last['x']},{last['y']}")
            joined = True
        drawn.append({**line, "path": " ".join(path), "last": last})
    points = []
    for d in dates:
        on_date = [values.get((line["name"], d)) for line in lines]
        shown_y = next((y(v) for v in on_date if v is not None), None)
        points.append({
            "x": x(d), "y": shown_y, "date": d,
            "rows": [[line["name"], value_text(v)] for line, v in zip(lines, on_date) if v is not None],
        })
    return {
        "lines": drawn,
        "points": points,
        "y_ticks": [{"y": y(t), "label": compact(t)} for t in ticks],
        "x_ticks": x_ticks,
    }


def time_axis(dates: list[dt.date]) -> tuple:
    """
    The x of an ISO date (a percentage, from the first to the last date) and the
    ticks: the start of each month, or the first and last day within a single month.
    """
    first, last = dates[0], dates[-1]
    span = max((last - first).days, 1)

    def x(d: dt.date | str) -> float:
        d = dt.date.fromisoformat(d) if isinstance(d, str) else d
        return 50.0 if first == last else round(100 * (d - first).days / span, 2)

    month_starts = [
        dt.date(yr, mo, 1)
        for yr in range(first.year, last.year + 1)
        for mo in range(1, 13)
        if first <= dt.date(yr, mo, 1) <= last
    ]
    year_starts = [m for m in month_starts if m.month == 1]
    if len(year_starts) >= 3:  # a long span: the start of each year, labelled by its year
        every = math.ceil(len(year_starts) / 8)
        x_ticks = [{"x": x(m), "label": m.strftime("%Y")} for m in year_starts[::every]]
    elif len(month_starts) >= 2:
        every = math.ceil(len(month_starts) / 8)
        x_ticks = [{"x": x(m), "label": m.strftime("%b %Y")} for m in month_starts[::every]]
    else:
        x_ticks = [{"x": x(d), "label": d.strftime("%b %d")} for d in sorted({first, last})]
    return x, x_ticks


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


def stacked_chart(monthly: list[tuple[str, dict[str, float]]], groups: dict[str, list[str]]) -> dict | None:
    """
    A 100% stacked area of the month-end balances of each group of accounts (in
    order, the first one at the bottom), as SVG paths in plot percentages.
    """
    months = [(m, {g: sum(max(b.get(a, 0.0), 0.0) for a in accs) for g, accs in groups.items()})
              for m, b in monthly]
    months = [(m, totals) for m, totals in months if sum(totals.values()) > 1e-9]
    if not months:
        return None
    x, x_ticks = time_axis([dt.date.fromisoformat(f"{m}-01") for m, _ in months])
    xs = [x(f"{m}-01") for m, _ in months]
    shares = [{g: v / sum(totals.values()) for g, v in totals.items()} for _, totals in months]
    if len(xs) == 1:  # a single month: a full-width band
        xs, shares = [0.0, 100.0], shares * 2
    areas, below = [], [0.0] * len(xs)
    for slot, group in enumerate(groups):
        above = [b + s[group] for b, s in zip(below, shares)]
        top = [f"{px},{round(100 * (1 - v), 2)}" for px, v in zip(xs, above)]
        bottom = [f"{px},{round(100 * (1 - v), 2)}" for px, v in zip(xs, below)][::-1]
        areas.append({
            "name": group, "slot": slot % 8 + 1,
            "path": "M" + " L".join(top + bottom) + " Z",
            "last": shares[-1][group],
        })
        below = above
    return {
        "areas": areas,
        "points": [
            {"x": x(f"{m}-01"), "date": m, "rows": [[g, f"{100 * v / sum(t.values()):.1f}%"] for g, v in t.items()][::-1]}
            for m, t in months
        ],
        "y_ticks": [{"y": 100 - t, "label": f"{t}%"} for t in (0, 25, 50, 75, 100)],
        "x_ticks": x_ticks,
        "first": months[0][0],
        "last": months[-1][0],
    }


def balance_groupings(snapshot: Snapshot, balances: dict[str, float]) -> dict:
    """
    Composition of the balances of assets and liabilities, grouped by currency (base
    first) or by liquidity, listing every group that has accounts. Accounts keep the
    order they are defined in.
    """
    def grouped(acc_type: AccountType, key, groups: list) -> list[dict]:
        accs = [a for a in snapshot.accounts.values() if a.acc_type == acc_type]
        return grouped_composition({
            name: {a.name: balances.get(a.name, 0.0) for a in accs if key(a) == group}
            for group, name in groups
            if any(key(a) == group for a in accs)
        })

    by_currency = [(c, c) for c in snapshot.currencies]
    by_liquidity = list(LIQUIDITY_GROUPS.items())
    return {
        "currency": {
            "assets": grouped(AccountType.ASSET, lambda a: a.currency.name, by_currency),
            "liabilities": grouped(AccountType.LIABILITY, lambda a: a.currency.name, by_currency),
        },
        "liquidity": {
            "assets": grouped(AccountType.ASSET, lambda a: a.is_liquid, by_liquidity),
            "liabilities": grouped(AccountType.LIABILITY, lambda a: a.is_liquid, by_liquidity),
        },
    }


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


def over_time_charts(snapshot: Snapshot, month: str = "") -> dict:
    """
    Month-end charts up to `month` (all the ledger by default): liabilities over
    equity and net liquidity (liquid assets minus liquid liabilities) over equity.
    """
    monthly = month_end_balances(snapshot, month)
    debt = ratio_series(snapshot, monthly, lambda b, total: total(b, AccountType.LIABILITY))
    liquidity = ratio_series(
        snapshot, monthly,
        lambda b, total: total(b, AccountType.ASSET, True) - total(b, AccountType.LIABILITY, True),
    )
    return {
        "debt_ratio": line_chart(debt, percent),
        "debt_series": debt,
        "liquidity_ratio": line_chart(liquidity, percent),
        "liquidity_series": liquidity,
    }


AVERAGE_MONTH_DAYS = 365.25 / 12
MAX_CHART_POINTS = 400  # long daily series are sampled down to about this many points to draw


def value_series(values: Series) -> list[dict]:
    """The {date, value} points of a series by date, with the change over the previous one (a ratio)."""
    series, prev = [], None
    for date, value in values.items():
        series.append({
            "date": date.date().isoformat(), "value": float(value),
            "change": value / prev - 1 if prev else None,
        })
        prev = value
    return series


def sampled(values: Series, limit: int = MAX_CHART_POINTS) -> Series:
    """Every k-th value of a series, so there are at most about `limit`, always with the last one."""
    step = math.ceil(len(values) / limit) if len(values) > limit else 1
    picked = list(range(0, len(values), step))
    if picked and picked[-1] != len(values) - 1:
        picked.append(len(values) - 1)
    return values.iloc[picked]


def growth(values: Series) -> dict | None:
    """
    How much a series by date grew from its first value to its last one (a ratio),
    and the same growth as an average per month. None without two dates.
    """
    if len(values) < 2:
        return None
    total = float(values.iloc[-1] / values.iloc[0])
    days = (values.index[-1] - values.index[0]).days
    return {
        "first": values.index[0].date().isoformat(),
        "total": total - 1,
        "monthly": total ** (AVERAGE_MONTH_DAYS / days) - 1,
    }


def real_values(values: Series, cpi: Series) -> tuple[Series, str | None]:
    """
    Values in the base currency deflated by the CPI of their dates, in the base
    currency of the last of their dates with a CPI (also returned). NaN on the dates
    without a CPI.
    """
    on_dates = cpi.reindex(values.index)
    known = on_dates.dropna()
    if not len(known):
        return values * math.nan, None
    return values * known.iloc[-1] / on_dates, known.index[-1].date().isoformat()


def inflation_months(base: Currency, today: dt.date) -> list[dict]:
    """
    The inflation of each month of the base currency, with the CPI at its end and the
    inflation of the 12 months up to it (None before 12 months). A month that hasn't
    ended by `today` is `projected`: its inflation can't be known yet.
    """
    inflation, cpi = base.monthly_inflation, base.customer_price_index
    current = Period(today, freq="M")
    yearly = (1 + inflation).rolling(12).apply(lambda g: g.prod(), raw=True) - 1
    return [
        {
            "month": str(month),
            "value": float(value),
            "cpi": float(cpi[month.end_time.normalize()]),
            "last_12": None if math.isnan(yearly[month]) else float(yearly[month]),
            "projected": month >= current,
        }
        for month, value in inflation.items()
    ]


def cpi_chart(base: Currency, today: dt.date) -> dict | None:
    """
    The daily CPI of the base currency, sampled to draw: one line up to the end of
    the last month that has ended by `today`, and a dashed one, projected, after it.
    """
    cpi = base.customer_price_index
    boundary = Timestamp(today.replace(day=1)) - Timedelta(days=1)  # the end of the last month ended

    def line(name: str, values: Series, dashed: bool) -> dict:
        return {"name": name, "dashed": dashed, "series": [
            {"date": d.date().isoformat(), "value": float(v)} for d, v in sampled(values).items()
        ]}

    lines = [line("CPI", cpi[:boundary], False), line("Projected", cpi[boundary:], True)]
    return multi_line_chart([ln for ln in lines if ln["series"]])


def build_currencies(snapshot: Snapshot, today: dt.date | None = None) -> dict:
    """
    The currencies: the daily CPI of the base currency and its inflation in each month
    (projected for the months that haven't ended by `today`), and, for each other
    currency, its value in the base currency (nominal) and that value deflated by the
    CPI (real) over time; the balances of the accounts in each currency, and the share
    of equity in the other currencies (None without them or positive equity); and the
    share of each currency in the assets at each month end.
    """
    today = today or dt.date.today()
    base = snapshot.base_currency
    cpi = base.customer_price_index
    balances = snapshot.ledger().balances()
    # The results of revaluing each account in another currency, all time.
    results: dict[str, float] = {}
    for move in snapshot.ledger(snapshot.result_rows).movements:
        gain = move.move_type == MovementType.GAIN
        name = move.acc_debit.name if gain else move.acc_credit.name
        results[name] = results.get(name, 0.0) + (move.amount if gain else -move.amount)

    def totals(name: str) -> dict:
        accs = [
            a for a in snapshot.accounts.values()
            if a.currency.name == name and a.acc_type != AccountType.EQUITY
        ]
        own = [a for a in accs if a.name in snapshot.other_balances]
        return {
            "accounts": len(accs),
            # The accounts revalued in this currency (see store._result_rows), with
            # their balance in it and in the base currency.
            "own": [
                {
                    "name": a.name, "description": a.description, "type": a.acc_type.value,
                    "balance": snapshot.other_balances[a.name], "base_balance": balances.get(a.name, 0.0),
                    "result": results.get(a.name, 0.0),
                }
                for a in own
            ],
            "assets": sum(balances.get(a.name, 0.0) for a in accs if a.acc_type == AccountType.ASSET),
            "liabilities": sum(
                balances.get(a.name, 0.0) for a in accs if a.acc_type == AccountType.LIABILITY
            ),
        }

    # Each currency is charted in the series color of its order, the base one (the CPI) first.
    others = []
    for i, c in enumerate(snapshot.currencies.values()):
        if c.is_base_currency:
            continue
        values = c.base_currency_mapping
        real, real_date = real_values(values, cpi)
        series = [
            {**p, "real": None if math.isnan(r) else float(r)}
            for p, r in zip(value_series(values), real)
        ]
        others.append({
            "name": c.name,
            "description": c.description,
            "slot": i % 8 + 1,
            **totals(c.name),
            "last": series[-1] if series else None,
            "growth": growth(values),
            "real_growth": growth(real.dropna()),
            "real_date": real_date,
            "series": series,
            "chart": multi_line_chart([
                {"name": "Nominal", "series": series, "dashed": False},
                {"name": "Real", "series": [{"date": p["date"], "value": p["real"]} for p in series], "dashed": True},
            ]),
        })

    months = inflation_months(base, today)
    known = [m for m in months if not m["projected"]]
    monthly = month_end_balances(snapshot)
    assets = [a for a in snapshot.accounts.values() if a.acc_type == AccountType.ASSET]
    shares = {
        c: [a.name for a in assets if a.currency.name == c]
        for c in snapshot.currencies
        if any(a.currency.name == c for a in assets)
    }
    base_totals = totals(base.name)
    # The share of equity (assets minus liabilities) in the accounts in other currencies, today
    other_equity = sum(o["assets"] - o["liabilities"] for o in others)
    equity = base_totals["assets"] - base_totals["liabilities"] + other_equity
    return {
        "base": {"name": base.name, "description": base.description, **base_totals},
        "others": others,
        "other_equity": other_equity,
        "other_equity_share": other_equity / equity if equity > 1e-9 and others else None,
        "last_month": known[-1] if known else None,  # the last month ended
        "last_12_monthly": (
            (1 + known[-1]["last_12"]) ** (1 / 12) - 1 if known and known[-1]["last_12"] is not None else None
        ),
        "cpi_chart": cpi_chart(base, today),
        "inflation_months": months,
        "inflation_chart": column_chart(months, percent) if months else None,
        "currency_shares": stacked_chart(monthly, shares),
    }
