"""views.py"""

from __future__ import annotations

import datetime as dt
import math

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
    DEBT,
    INVESTMENT,
    SPECIAL_LABELS,
    CreditCardAccount,
    Currency,
    DebtAccount,
    InvestmentAccount,
    next_month,
    rounds_to_zero,
    special_of,
)
from pledger.web.store import (
    BASE_AMOUNT,
    INITIAL_GAIN,
    INTEREST,
    INVESTMENT_COLUMN,
    OTHER_AMOUNT,
    QUANTITY_COLUMN,
    QUANTITY_OPTIONAL,
    QUANTITY_REQUIRED,
    Snapshot,
    Work,
    conversion_fingerprint,
    interest_fingerprint,
    is_capital,
    row_fingerprint,
    work_fingerprint,
)

ACCOUNT_TYPE_ORDER = list(AccountType)

# The tab where each kind of movement that can't be deleted from the Ledger tab is
# handled: its colored circle there, the link to it and its tooltip.
MOVEMENT_SOURCES = {
    "investments": ("🟢", "/investments", "Added and deleted from the Investments tab"),
    "currencies": ("🔵", "/currencies", "Added and deleted from the Currencies tab"),
    "revaluation": ("🔵", "/currencies", "Generated from the values of the Currencies tab"),
    "work": ("🟠", "/work", "Generated each week from the gross salary of the Work tab"),
    "accounts": ("🟣", "/accounts", "Generated from the initial amount of the account, in the Accounts tab"),
    "debt": ("🟡", "/debt", "A debt movement, shown in the Debt tab; it can only be changed in the ledger CSV"),
}


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
        if move.move_base.internal and DEBT in {move.move_base.debit_special, move.move_base.credit_special}:
            return "debt"
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
        }
        for (ix, row), move in zip(rows.iterrows(), ledger.movements)
    ]
    movements.reverse()
    return movements


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

    return {
        "months": months_of(snapshot),
        "month": month,
        "account": account,
        "initial": initial,
        "accounts": list(snapshot.accounts),
        "movements": movement_rows(snapshot, filtered, ledger),
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
        "groupings": balance_groupings(snapshot, balances),
        **over_time_charts(snapshot, month),
        # New equity accounts are not supported for now.
        "account_types": [t.value for t in ACCOUNT_TYPE_ORDER if t != AccountType.EQUITY],
        "currencies": list(snapshot.currencies.values()),
        "check_labels": CHECK_LABELS,
        "values": values,
    }


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


def build_credit_cards(snapshot: Snapshot, month: str = "") -> dict:
    """
    The balance of each credit card account (what we owe on it) as of the end of
    `month` (all the ledger by default), and what was charged to it (credits) and
    paid (debits) in that month, leaving out the initial movements. Lists the
    cards with a negative balance (we are owed): rare, so possibly an error. Charts
    the net owed on them over equity at each month end, and what was charged to them
    in each month, up to `month`.
    """
    cards = [a for a in snapshot.accounts.values() if isinstance(a, CreditCardAccount)]
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
        "owed": sum(a["balance"] for a in accounts),
        "charged": sum(a["charged"] for a in accounts),
        "paid": sum(a["paid"] for a in accounts),
        "composition": composition({a["name"]: a["balance"] for a in accounts}, ordered=True),
        "negative": [a for a in accounts if a["balance"] < 0 and not rounds_to_zero(a["balance"])],
        "movements": account_movements(snapshot, [a.name for a in cards], month),
        **owed_ratio_chart(snapshot, [a.name for a in cards], month),
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


def account_movements(snapshot: Snapshot, names: list[str], month: str = "") -> list[dict]:
    """
    The movements with any of these accounts on a side, newest first, in `month`
    (all the ledger by default), the initial movements included.
    """
    rows = snapshot.all_rows
    mask = rows.acc_debit.isin(names) | rows.acc_credit.isin(names)
    if month:
        mask &= rows.move_date.str.startswith(month)
    filtered = rows[mask]
    movements = movement_rows(snapshot, filtered, snapshot.ledger(filtered))
    for m in movements:
        m["initial"] = m["base"] in snapshot.initial_base_names
    return movements


def owed_ratio_chart(snapshot: Snapshot, cards: list[str], month: str = "") -> dict:
    """The net owed on the credit cards over equity at each month end, up to `month`."""
    owed = ratio_series(
        snapshot, month_end_balances(snapshot, month),
        lambda b, _total: sum(b.get(name, 0.0) for name in cards),
    )
    return {"owed_ratio": line_chart(owed, percent), "owed_series": owed}


def build_invest_movements(snapshot: Snapshot) -> list[dict]:
    """
    The movements of the investment base movements, newest first: those of the
    investments CSV, with their investment and quantity, and those of the interest
    CSV. Their `index` is their row in the CSV of their `source`.
    """
    movements = []
    for source, rows, generated, fingerprint in (
        ("investments", snapshot.investments, snapshot.investment_rows, row_fingerprint),
        ("interest", snapshot.interest, snapshot.interest_rows, interest_fingerprint),
    ):
        # movement_rows lists them newest first: back to the order of the CSV
        moves = movement_rows(snapshot, generated, snapshot.ledger(generated))[::-1]
        for i, (m, (_, row)) in enumerate(zip(moves, rows.iterrows())):
            movements.append({
                **m, "index": i, "fingerprint": fingerprint(row), "source": source,
                "investment": row.get(INVESTMENT_COLUMN, ""), "quantity": row.get(QUANTITY_COLUMN, ""),
            })
    movements.sort(key=lambda m: m["date"])  # stable: in their order on each date
    movements.reverse()
    return movements


def build_conversions(snapshot: Snapshot) -> list[dict]:
    """
    The rows of the conversions CSV, newest first, with the amount in each currency
    and the value of the other currency in the base one they imply.
    """
    conversions = []
    for i, (ix, row) in enumerate(snapshot.conversions.iterrows()):
        base_amount = float(row[BASE_AMOUNT].replace(",", "") or 0)
        other_amount = float(row[OTHER_AMOUNT].replace(",", "") or 0)
        debit, credit = snapshot.accounts[row["acc_debit"]], snapshot.accounts[row["acc_credit"]]
        other = debit if not debit.currency.is_base_currency else credit
        conversions.append({
            "index": i,
            "fingerprint": conversion_fingerprint(row),
            "date": row["move_date"],
            "base": row["base_movement"],
            "debit": debit.name,
            "credit": credit.name,
            "amount": base_amount,
            "other_amount": other_amount,
            "currency": other.currency.name,
            "bought": other is debit,  # the other currency comes in, else goes out
            "rate": base_amount / other_amount if other_amount else None,
            "comment": row.get("comment", ""),
        })
    conversions.sort(key=lambda c: c["date"])  # stable: in their order on each date
    conversions.reverse()
    return conversions


def conversion_fields(snapshot: Snapshot, base_movement: str = "") -> dict:
    """
    The transfers to pick from for a conversion, and the account fields of the
    selected one: by default, that of the last conversion, else the first one.
    """
    bases = sorted(
        n for n, b in snapshot.manual_base_movements().items() if b.move_type == MovementType.TRANSFER
    )
    if base_movement not in bases:
        last = snapshot.conversions["base_movement"].iloc[-1] if len(snapshot.conversions) else ""
        base_movement = last if last in bases else next(iter(bases), "")
    return {
        "bases": bases,
        "base_movement": base_movement,
        "fields": account_fields(snapshot, base_movement),
        "other_currencies": [c.name for c in snapshot.currencies.values() if not c.is_base_currency],
    }


def invest_fields(snapshot: Snapshot, base_movement: str = "") -> dict:
    """
    The investment base movements to pick from, by movement type, and the fields
    of the selected one (the first one by default): its accounts, whether it names
    an investment ("required" with an INVESTMENT side, else "optional") and has a
    quantity ("required", "optional" or None), plus the known investments and the
    investment types, for a new investment.
    """
    bases = snapshot.invest_base_movements()
    by_type = {
        t.value: [n for n in bases if bases[n].move_type == t]
        for t in MovementType
        if any(b.move_type == t for b in bases.values())
    }
    if base_movement not in bases:
        base_movement = next(iter(bases), "")
    base = bases.get(base_movement)
    side = base is not None and INVESTMENT in {base.debit_special, base.credit_special}
    return {
        "bases_by_type": by_type,
        "base_movement": base_movement,
        "fields": account_fields(snapshot, base_movement),
        "investment": None if base_movement in INTEREST else "required" if side else "optional",
        "quantity": (
            "required" if base_movement in QUANTITY_REQUIRED
            else "optional" if base_movement in QUANTITY_OPTIONAL
            else None
        ),
        "investment_names": sorted({n for a in snapshot.investment_accounts() for n in a.investments}),
        "investment_types": INVESTMENT_TYPE_LABELS if side else {},
    }


CREDITOR_KIND_LABELS = {
    CreditorKind.PERSON: "People", CreditorKind.SERVICE: "Services", CreditorKind.COMPANY: "Companies",
}


def build_debts(snapshot: Snapshot, month: str = "") -> dict:
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
        kinds.append({"kind": kind.value, "title": title, "rows": rows, "net": sum(r["amount"] for r in rows)})

    # Each movement is about whoever its comment names, in its debt account.
    movements = account_movements(snapshot, [a.name for a in accounts], month)
    for m in movements:
        debt = next(a for a in accounts if a.name in (m["debit"], m["credit"]))
        m["account"] = debt.name
        m["who"] = debt.creditor_in(m["comment"])

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
        "we_owe": we_owe,
        "owed_to_us": owed_to_us,
        "unassigned": unassigned,
        "has_unassigned": not rounds_to_zero(unassigned),
        "net": we_owe - owed_to_us + unassigned,
        "creditors": by_kind(DebtAccount.get_creditors, 1),
        "debtors": by_kind(DebtAccount.get_debtors, -1),
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


def build_work(work: Work, form: dict | None = None, cpi: Series | None = None) -> dict:
    """
    The companies worked for and their job positions, every month of the work
    history (the newest first) and the totals of this year, plus the form state.
    Worked months already over with no received payment date are flagged as unpaid.
    With the `cpi` of the base currency, the salary charts also get their real values.
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
        "gains": by_result_type(MovementType.GAIN),
        "losses": by_result_type(MovementType.LOSS),
        "statement": statement,
        "waterfall": waterfall_chart(statement),
        "equity": equity,
        "equity_series": series,
    }


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
    CPI (real) over time; the balances of the accounts in each currency; and the
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
    return {
        "base": {"name": base.name, "description": base.description, **totals(base.name)},
        "others": others,
        "last_month": known[-1] if known else None,  # the last month ended
        "last_12_monthly": (
            (1 + known[-1]["last_12"]) ** (1 / 12) - 1 if known and known[-1]["last_12"] is not None else None
        ),
        "cpi_chart": cpi_chart(base, today),
        "inflation_months": months,
        "inflation_chart": column_chart(months, percent) if months else None,
        "currency_shares": stacked_chart(monthly, shares),
    }
