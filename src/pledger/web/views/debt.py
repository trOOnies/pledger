"""debt.py: the People tab: who we owe and who owes us, and the debt movement form."""

from __future__ import annotations

import math
from dataclasses import replace

from pledger.enums import CreditorKind, DebtRole, LedgerSide
from pledger.models import (
    Account,
    Creditor,
    DebtAccount,
    is_foreign,
    month_last_day,
    rounds_to_zero,
)
from pledger.web.store import (
    DEBT_BASE_ROLES,
    DEBT_BASE_SIDES,
    DEBT_BASES,
    DEBT_INCURRED,
    DEBT_PAID,
    DIFFERENCES,
    LOAN_COLLECTED,
    LOAN_GIVEN,
    RESULT_GAIN,
    RESULT_LOSS,
    Snapshot,
)
from pledger.web.store.common import _parse_amount, _value_on
from pledger.web.store.movements import debt_amount
from pledger.web.views.common import account_movements, composition, months_of
from pledger.web.views.forms import account_fields

CREDITOR_KIND_LABELS = {
    CreditorKind.PERSON: "People", CreditorKind.SERVICE: "Services", CreditorKind.COMPANY: "Companies",
}


CREDITOR_KIND_EMOJIS = {CreditorKind.PERSON: "👥", CreditorKind.SERVICE: "🧾", CreditorKind.COMPANY: "🏢"}


UNASSIGNED = "-"  # the who filter of the People tab for the movements naming no one


# What debt_after gives a movement it leaves out (initial or a revaluation)
NO_DEBT = {"debt": None, "debt_currency": None, "own_amount": None, "own_currency": None}


def as_of(snapshot: Snapshot, month: str = "") -> str:
    """The date the People tab values the debt accounts in other currencies at: the end of `month`, else today."""
    return month_last_day(month).isoformat() if month else snapshot.today.isoformat()


def in_base(account: Account, amount: float, date: str) -> float:
    """
    An amount in the currency of an account, in the base currency at the value of the
    currency on that date (the last one up to then): NaN without any value of it.
    """
    if account.currency.is_base_currency or rounds_to_zero(amount):
        return amount
    try:
        return amount * _value_on(account.currency, date, "")
    except ValueError:
        return math.nan


def own_currency(account: Account) -> str | None:
    """The currency of the amounts of an account shown in it: None for the base one (see money_in)."""
    return account.currency.name if is_foreign(account) else None


def net_creditors(
    snapshot: Snapshot, accounts: list[DebtAccount], month: str = "",
) -> tuple[dict[str, Creditor], dict[str, dict[str, float]]]:
    """
    Everyone of these debt accounts, once (they share the creditors YAML), with what we
    owe them in all of them (negative: what they owe us), in the base currency, at the
    value of the other currencies at the end of `month` (see as_of), and what we owe them
    in each one they can be in, in its currency, by account name.
    """
    date = as_of(snapshot, month)
    first: dict[str, Creditor] = {}
    amounts: dict[str, dict[str, float]] = {}
    net: dict[str, float] = {}
    for a in accounts:
        for n, c in a.creditors.items():
            first.setdefault(n, c)
            amounts.setdefault(n, {})[a.name] = c.amount
            net[n] = net.get(n, 0.0) + in_base(a, c.amount, date)
    return {n: replace(c, amount=net[n]) for n, c in first.items()}, amounts


def shown_accounts(snapshot: Snapshot, accounts: list[DebtAccount]) -> list[DebtAccount]:
    """
    The debt accounts the People tab has a column for: those in the base currency, and those
    in another currency with any movement or amount owed.
    """
    rows = snapshot.all_rows
    used = set(rows.acc_debit) | set(rows.acc_credit)
    return [
        a for a in accounts
        if a.currency.is_base_currency or a.name in used
        or not rounds_to_zero(a.unassigned) or any(not rounds_to_zero(c.amount) for c in a.creditors.values())
    ]


def role_of(c: Creditor) -> str:
    return "Creditor" if c.is_creditor else "Debtor" if c.is_debtor else "Settled"


def by_currency(
    snapshot: Snapshot, accounts: list[DebtAccount], people: dict[str, Creditor], amounts: dict[str, dict[str, float]],
) -> list[dict]:
    """
    The tables of people, services and companies of each currency, with what we owe each one
    in its debt and loan accounts and in both, in that currency, and their role there. Those
    of the base currency have everyone, and those of another one whoever its movements named
    (see DebtAccount.named). Tables, and currencies, without anyone are left out.
    """
    found = []
    for currency in snapshot.currencies.values():
        accs = [a for a in accounts if a.currency.name == currency.name]
        if not accs:
            continue
        used = set().union(*(a.named for a in accs))
        tables = []
        for kind, title in CREDITOR_KIND_LABELS.items():
            rows = []
            for c in people.values():
                if c.kind != kind or not (currency.is_base_currency or c.name in used):
                    continue
                own = {a.name: amounts[c.name].get(a.name) for a in accs}
                here = replace(c, amount=sum(v or 0.0 for v in own.values()))
                rows.append({
                    "name": c.name, "role": role_of(here),
                    "expected": c.expected.value.capitalize() if c.expected else None,
                    "met": here.meets_expected, "amount": here.amount, "amounts": own,
                })
            if rows:
                tables.append({
                    "kind": kind.value, "title": title, "emoji": CREDITOR_KIND_EMOJIS[kind], "rows": rows,
                    "net": sum(r["amount"] for r in rows),
                    "nets": {a.name: sum(r["amounts"][a.name] or 0.0 for r in rows) for a in accs},
                })
        if tables:
            found.append({
                "currency": currency.name, "own": own_currency(accs[0]),
                "accounts": [{"name": a.name, "description": a.description} for a in accs], "kinds": tables,
            })
    return found


def build_debts(snapshot: Snapshot, month: str = "", who: str = "") -> dict:
    """
    The People tab: the people of their debt accounts, those of the currencies (the
    services and the companies have theirs, in the Services and Work tabs): the ones we
    owe (creditors) and the ones that owe us (debtors), in all of them, with what we owe
    and are owed as positive amounts, in the base currency (see net_creditors), plus the
    debt of the movements whose comment names no one (unassigned), as of the end of
    `month` (all the ledger by default). What is owed in each account is in its currency.
    Lists who isn't in their expected role, the unassigned debt of each account by
    comment, and the movements naming someone who isn't in their account, but in another
    debt account (misplaced).
    """
    date = as_of(snapshot, month)
    all_debts = snapshot.debt_accounts(month)
    every = [a for a in all_debts if a.kind == CreditorKind.PERSON]
    people, amounts = net_creditors(snapshot, every, month)
    accounts = shown_accounts(snapshot, every)

    def owed_by(sign: int) -> list[dict]:
        def owed(c: Creditor) -> bool:
            return c.is_creditor if sign > 0 else c.is_debtor

        return composition({n: sign * c.amount for n, c in people.items() if owed(c)}, limit=None)

    currency_tables = by_currency(snapshot, every, people, amounts)

    # Each movement is about whoever its comment names, in its debt account.
    by_name = {a.name: a for a in every}
    movements = account_movements(
        snapshot, list(by_name), month, editable=snapshot.debt_base_movements(),
        deletable=snapshot.difference_base_movements(),
    )
    misplaced = []
    for m in movements:
        debt = next(a for a in every if a.name in (m["debit"], m["credit"]))
        m["account"] = debt.name
        m["revaluation"] = m["base"] in (RESULT_GAIN, RESULT_LOSS)
        m["who"] = None if m["revaluation"] else debt.creditor_in(m["comment"])
        other = next((a for a in all_debts if a.name != debt.name and a.creditor_in(m["comment"])), None)
        if m["who"] is None and not m["revaluation"] and other is not None:
            misplaced.append({**m, "names": other.creditor_in(m["comment"]), "belongs": other.name})
    after = debt_after(snapshot, every)
    for m in movements:
        m |= after.get(m["index"], NO_DEBT)
    # Who to pick in the filter of the movements
    who_options = sorted(people)
    names = set(who_options)
    if who not in names and who != UNASSIGNED:
        who = ""
    if who:
        movements = [m for m in movements if not m["revaluation"] and (m["who"] or UNASSIGNED) == who]

    we_owe = sum(c.amount for c in people.values() if c.is_creditor)
    owed_to_us = -sum(c.amount for c in people.values() if c.is_debtor)
    unassigned = sum(in_base(a, a.unassigned, date) for a in every)
    return {
        "accounts": [
            {
                "name": a.name,
                "description": a.description,
                "currency": a.currency.name,
                "own": own_currency(a),
                "role": "Debts" if a.role == DebtRole.CREDITOR else "Loans",
                "people": len(a.creditors),
                "unassigned": a.unassigned,
                "net": (net := sum(c.amount for c in a.creditors.values()) + a.unassigned),
                "base_net": in_base(a, net, date),
            }
            for a in accounts
        ],
        "has_foreign": any(is_foreign(a) for a in accounts),
        "base_currency": snapshot.base_currency.name,
        "currency_tables": currency_tables,
        "months": months_of(snapshot),
        "month": month,
        "unmet": [
            {
                "name": c.name, "role": role_of(c), "amount": c.amount,
                "expected": c.expected.value.capitalize() if c.expected else None,
            }
            for c in people.values() if not c.meets_expected
        ],
        "unassigned_accounts": [
            {
                "name": a.name,
                "own": own_currency(a),
                "amount": a.unassigned,
                "comments": sorted(
                    ((c or "(no comment)", v) for c, v in a.unassigned_comments.items()
                     if not rounds_to_zero(v)),
                    key=lambda cv: -abs(cv[1]),
                ),
            }
            for a in every
            if not rounds_to_zero(a.unassigned)
        ],
        "multiple_accounts": len(every) > 1,
        "misplaced": misplaced,
        "movements": movements,
        "who": who,
        "who_options": who_options,
        "we_owe": we_owe,
        "owed_to_us": owed_to_us,
        "unassigned": unassigned,
        "has_unassigned": not rounds_to_zero(unassigned),
        "net": we_owe - owed_to_us + unassigned,
        "creditors": owed_by(1),
        "debtors": owed_by(-1),
    }


def debt_after(snapshot: Snapshot, accounts: list[DebtAccount]) -> dict[int, dict]:
    """
    What we owe whoever each movement of these debt accounts is about (negative: what
    they owe us), or the unassigned debt of its account if it names no one, right after
    it, in the currency of its account, by its index in all_rows: from their initial
    amounts in the creditors YAML, through all the ledger, like _apply_debts. Each one also
    has its amount and currency in a debt account in another currency. The initial
    movements are left out, and so are the revaluations, which owe nothing more.
    """
    initial = {a.name: a for a in snapshot.accounts.values() if isinstance(a, DebtAccount)}
    debts: dict[tuple[str, str | None], float] = {
        (a.name, n): c.amount for a in initial.values() for n, c in a.creditors.items()
    }
    rows = snapshot.all_rows
    after: dict[int, dict] = {}
    for m in account_movements(snapshot, [a.name for a in accounts])[::-1]:
        if m["initial"] or m["base"] in (RESULT_GAIN, RESULT_LOSS):
            continue
        row = rows.loc[m["index"]]
        for name, sign in ((m["debit"], -1), (m["credit"], 1)):
            if name in initial:
                account = initial[name]
                key = (name, account.creditor_in(m["comment"]))
                own = debt_amount(snapshot, account, row)
                debts[key] = debts.get(key, 0.0) + sign * own
                after[m["index"]] = {
                    "debt": debts[key], "debt_currency": own_currency(account),
                    "own_amount": own if is_foreign(account) else None, "own_currency": own_currency(account),
                }
    return after


def kind_movements(snapshot: Snapshot, kind: CreditorKind, who: str = "") -> tuple[list[DebtAccount], list[dict]]:
    """
    The debt accounts of the services (SeD and SeL) or of the companies (Wo), with all the
    ledger applied, and their movements, newest first (about `who` only, if any): each
    with its account, who its comment names and what we owe them right after it (see
    debt_after). Those of the debt base movements and their differences can be deleted
    (see the delete route of the debt movements).
    """
    accounts = snapshot.debt_accounts(kind=kind)
    by_name = {a.name: a for a in accounts}
    movements = account_movements(
        snapshot, list(by_name), deletable={**snapshot.debt_base_movements(), **snapshot.difference_base_movements()},
    )
    after = debt_after(snapshot, accounts)
    for m in movements:
        debt = by_name.get(m["debit"]) or by_name[m["credit"]]
        m |= {"account": debt.name, "revaluation": False, "who": debt.creditor_in(m["comment"])}
        m |= after.get(m["index"], NO_DEBT)
    if who:
        movements = [m for m in movements if (m["who"] or UNASSIGNED) == who]
    return accounts, movements


# The base movements of the People tab, in two kinds: the debt we owe and the loans we give,
# with the color of their side, the label of the button of each base movement and the role
# of their debt accounts (see DEBT_BASE_ROLES).
DEBT_PAIRS = [
    ("Debt", "liability", {DEBT_INCURRED: "Incurred", DEBT_PAID: "Paid"}, DebtRole.CREDITOR),
    ("Loan", "asset", {LOAN_GIVEN: "Given", LOAN_COLLECTED: "Collected"}, DebtRole.DEBTOR),
]


def debt_form_fields(snapshot: Snapshot, base_movement: str = "", kind: str = "") -> dict:
    """
    The debt base movements to pick from in the People tab: first the kind (see DEBT_PAIRS),
    that of the base movement if any, else `kind`, else the first one; then the base
    movements of that kind, with the account fields of the selected one (the first one by
    default), whose side takes the debt accounts of the people of its role, in any
    currency (never those of the services or the companies, on either side). Also the ones
    of DEBT_BASES that aren't as expected, the people of the debt accounts of that kind a
    movement can be about, and the currency of every account, so the form shows each
    amount in its own.
    """
    bases, missing = snapshot.tab_base_movements(DEBT_BASES, DEBT_BASE_SIDES)
    kinds = [
        {
            "title": title, "color": color,
            # The second one (paid or collected) in a lighter color
            "buttons": {
                n: (label, color if i == 0 else f"{color}-light")
                for i, (n, label) in enumerate(labels.items()) if n in bases
            },
        }
        for title, color, labels, _ in DEBT_PAIRS
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
    role = DEBT_BASE_ROLES.get(base_movement)
    accounts = snapshot.role_accounts(role) if role else []
    who = {n for account in accounts for n in account.creditors}
    return {
        "kinds": [{"title": k["title"], "color": k["color"]} for k in kinds],
        "account": f"the {'debt' if role == DebtRole.CREDITOR else 'loan'} accounts" if accounts else "",
        "kind": picked["title"] if picked else "",
        "buttons": buttons,
        "bases": list(bases),
        "base_movement": base_movement,
        "missing": missing,
        "fields": people_fields(snapshot, account_fields(snapshot, base_movement)),
        "debt_side": "acc_credit" if DEBT_BASES.get(base_movement) == LedgerSide.CREDIT else "acc_debit",
        "who": sorted(who),
        "difference_missing": sorted(DIFFERENCES.keys() - snapshot.difference_base_movements().keys()),
        # Every account in another currency, its currency (see the debt form): the others are in the base one
        "account_currencies": {
            a.name: a.currency.name for a in snapshot.accounts.values() if not a.currency.is_base_currency
        },
        "base_currency": snapshot.base_currency.name,
        "foreign_debts": any(is_foreign(a) for a in snapshot.accounts.values() if isinstance(a, DebtAccount)),
    }


def people_fields(snapshot: Snapshot, fields: dict) -> dict:
    """The account fields of a debt base movement (see account_fields) without the debt accounts of the services and the companies."""
    others = {a.name for a in snapshot.accounts.values() if isinstance(a, DebtAccount) and a.kind != CreditorKind.PERSON}
    fields = dict(fields)
    for side in ("debit", "credit"):
        fields[f"{side}_options"] = [n for n in fields[f"{side}_options"] if n not in others]
        fields[f"{side}_groups"] = {
            g: kept for g, names in fields[f"{side}_groups"].items() if (kept := [n for n in names if n not in others])
        }
    return fields


def debt_difference(
    snapshot: Snapshot, base_movement: str, who: str, amount: str,
    acc_debit: str = "", acc_credit: str = "", amount_debt: str = "",
) -> dict:
    """
    What the debt movement form would do to the balance with `who` in its debt account,
    in its currency, positive if we owe them (see Snapshot.debt_remaining), and the
    Difference settling it, if any. The amount in that currency is `amount_debt`, else
    `amount` when the other account is in the same currency (or none is picked yet).
    Empty until the form has someone and a valid amount.
    """
    debt = snapshot.debt_account_of(base_movement, acc_debit, acc_credit)
    if debt is None:  # no debt account picked yet: the one in the base currency
        role = DEBT_BASE_ROLES.get(base_movement)
        debt = snapshot.base_debt_account(role) if role else None
    if debt is None or who not in debt.creditors:
        return {}
    other = snapshot.accounts.get(acc_credit if debt.name == acc_debit else acc_debit)
    text = amount_debt if amount_debt.strip() else (
        amount if other is None or other.currency.name == debt.currency.name else ""
    )
    if not text.strip():
        return {}
    try:
        value = _parse_amount(text, "amount")
    except ValueError:
        return {}
    balance, remaining = snapshot.debt_remaining(debt.name, base_movement, value, who)
    difference = snapshot.difference_of(remaining)
    return {
        "who": who, "account": debt.name, "own": own_currency(debt), "balance": balance, "remaining": remaining,
        "base": difference[0] if difference else None, "amount": difference[1] if difference else 0.0,
    }
