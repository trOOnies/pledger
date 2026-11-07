"""debt.py: the Debt tab: who we owe and who owes us, and the debt movement form."""

from __future__ import annotations

from dataclasses import replace

from pledger.enums import CreditorKind
from pledger.models import (
    DEBT_ACCOUNT,
    LOAN_ACCOUNT,
    Creditor,
    DebtAccount,
    rounds_to_zero,
)
from pledger.web.store import (
    DEBT_BASE_ACCOUNTS,
    DEBT_BASES,
    DEBT_INCURRED,
    DEBT_PAID,
    DIFFERENCES,
    LOAN_COLLECTED,
    LOAN_GIVEN,
    Snapshot,
)
from pledger.web.store.common import _parse_amount
from pledger.web.views.common import account_movements, grouped_composition, months_of
from pledger.web.views.forms import account_fields

CREDITOR_KIND_LABELS = {
    CreditorKind.PERSON: "People", CreditorKind.SERVICE: "Services", CreditorKind.COMPANY: "Companies",
}


CREDITOR_KIND_EMOJIS = {CreditorKind.PERSON: "👥", CreditorKind.SERVICE: "🧾", CreditorKind.COMPANY: "🏢"}


UNASSIGNED = "-"  # the who filter of the Debt tab for the movements naming no one


def net_creditors(accounts: list[DebtAccount]) -> tuple[dict[str, Creditor], dict[str, dict[str, float]]]:
    """
    Everyone of these debt accounts, once (they share the creditors YAML), with what we
    owe them in all of them (negative: what they owe us), and what we owe them in each
    one they can be in, by account name.
    """
    first: dict[str, Creditor] = {}
    amounts: dict[str, dict[str, float]] = {}
    for a in accounts:
        for n, c in a.creditors.items():
            first.setdefault(n, c)
            amounts.setdefault(n, {})[a.name] = c.amount
    return {n: replace(c, amount=sum(amounts[n].values())) for n, c in first.items()}, amounts


def build_debts(snapshot: Snapshot, month: str = "", who: str = "") -> dict:
    """
    The people, services and companies of the debt accounts: the ones we owe (creditors)
    and the ones that owe us (debtors), in all of them, with what we owe and are owed
    as positive amounts, plus the debt of the movements whose comment names no
    one (unassigned), as of the end of `month` (all the ledger by default). Lists
    who isn't in their expected role, the unassigned debt of each account by comment,
    and the movements naming someone who isn't in their account (misplaced).
    """
    accounts = snapshot.debt_accounts(month)
    people, amounts = net_creditors(accounts)

    def by_kind(sign: int) -> list[dict]:
        def owed(c: Creditor) -> bool:
            return c.is_creditor if sign > 0 else c.is_debtor

        return grouped_composition({
            title: {n: sign * c.amount for n, c in people.items() if c.kind == kind and owed(c)}
            for kind, title in CREDITOR_KIND_LABELS.items()
            if any(c.kind == kind and owed(c) for c in people.values())
        })

    kinds = []
    for kind, title in CREDITOR_KIND_LABELS.items():
        rows = [
            {
                "name": c.name,
                "role": "Creditor" if c.is_creditor else "Debtor" if c.is_debtor else "Settled",
                "expected": c.expected.value.capitalize() if c.expected else None,
                "met": c.meets_expected,
                "amount": c.amount,
                "amounts": {a.name: amounts[c.name].get(a.name) for a in accounts},
            }
            for c in people.values() if c.kind == kind
        ]
        kinds.append({
            "kind": kind.value, "title": title, "emoji": CREDITOR_KIND_EMOJIS[kind],
            "rows": rows, "net": sum(r["amount"] for r in rows),
            "nets": {a.name: sum(r["amounts"][a.name] or 0.0 for r in rows) for a in accounts},
        })

    # Each movement is about whoever its comment names, in its debt account.
    movements = account_movements(
        snapshot, [a.name for a in accounts], month, editable=snapshot.debt_base_movements(),
        deletable=snapshot.difference_base_movements(),
    )
    misplaced = []
    for m in movements:
        debt = next(a for a in accounts if a.name in (m["debit"], m["credit"]))
        m["account"] = debt.name
        m["who"] = debt.creditor_in(m["comment"])
        other = next((a for a in accounts if a is not debt and a.creditor_in(m["comment"])), None)
        if m["who"] is None and other is not None:
            misplaced.append({**m, "names": other.creditor_in(m["comment"]), "belongs": other.name})
    after = debt_after(snapshot, accounts)
    for m in movements:
        m["debt"] = after.get(m["index"])
    # Who to pick in the filter of the movements, by kind
    who_options = {
        title: sorted(n for n, c in people.items() if c.kind == kind) for kind, title in CREDITOR_KIND_LABELS.items()
    }
    names = {n for ns in who_options.values() for n in ns}
    if who not in names and who != UNASSIGNED:
        who = ""
    if who:
        movements = [m for m in movements if (m["who"] or UNASSIGNED) == who]

    we_owe = sum(c.amount for c in people.values() if c.is_creditor)
    owed_to_us = -sum(c.amount for c in people.values() if c.is_debtor)
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
        "misplaced": misplaced,
        "movements": movements,
        "who": who,
        "who_options": {title: ns for title, ns in who_options.items() if ns},
        "we_owe": we_owe,
        "owed_to_us": owed_to_us,
        "unassigned": unassigned,
        "has_unassigned": not rounds_to_zero(unassigned),
        "net": we_owe - owed_to_us + unassigned,
        "creditors": by_kind(1),
        "debtors": by_kind(-1),
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


# The base movements of the Debt tab, in two kinds: the debt we owe and the loans we give,
# with the color of their side, the label of the button of each base movement and their
# debt account (see DEBT_BASE_ACCOUNTS).
DEBT_PAIRS = [
    ("Debt", "liability", {DEBT_INCURRED: "Incurred", DEBT_PAID: "Paid"}, DEBT_ACCOUNT),
    ("Loan", "asset", {LOAN_GIVEN: "Given", LOAN_COLLECTED: "Collected"}, LOAN_ACCOUNT),
]


def debt_form_fields(snapshot: Snapshot, base_movement: str = "", kind: str = "") -> dict:
    """
    The debt base movements to pick from in the Debt tab: first the kind (see DEBT_PAIRS),
    that of the base movement if any, else `kind`, else the first one; then the base
    movements of that kind, with the account fields of the selected one (the first one by
    default). Also the ones of DEBT_BASES that aren't as expected, and who of the debt
    account of that kind a movement can be about, by kind: people and services, but no
    companies, whose debt comes from the salaries of the Work tab.
    """
    bases, missing = snapshot.tab_base_movements(DEBT_BASES, DEBT_BASE_ACCOUNTS)
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
    who: dict[str, set[str]] = {label: set() for label in CREDITOR_KIND_LABELS.values()}
    account = snapshot.accounts.get(DEBT_BASE_ACCOUNTS.get(base_movement, ""))
    if isinstance(account, DebtAccount):
        for c in account.creditors.values():
            if c.kind != CreditorKind.COMPANY:
                who[CREDITOR_KIND_LABELS[c.kind]].add(c.name)
    return {
        "kinds": [{"title": k["title"], "color": k["color"]} for k in kinds],
        "account": account.name if isinstance(account, DebtAccount) else "",
        "kind": picked["title"] if picked else "",
        "buttons": buttons,
        "bases": list(bases),
        "base_movement": base_movement,
        "missing": missing,
        "fields": account_fields(snapshot, base_movement),
        "who": {label: sorted(names) for label, names in who.items() if names},
        "difference_missing": sorted(DIFFERENCES.keys() - snapshot.difference_base_movements().keys()),
    }


def debt_difference(snapshot: Snapshot, base_movement: str, who: str, amount: str) -> dict:
    """
    What the debt movement form would do to the balance with `who`, positive if we owe them
    (see Snapshot.debt_remaining), and the Difference settling it, if any. Empty until the
    form has someone and a valid amount.
    """
    account = snapshot.accounts.get(DEBT_BASE_ACCOUNTS.get(base_movement, ""))
    known = isinstance(account, DebtAccount) and who in account.creditors
    if not known or not amount.strip():
        return {}
    try:
        value = _parse_amount(amount, "amount")
    except ValueError:
        return {}
    balance, remaining = snapshot.debt_remaining(base_movement, value, who)
    difference = snapshot.difference_of(remaining)
    return {
        "who": who, "balance": balance, "remaining": remaining,
        "base": difference[0] if difference else None, "amount": difference[1] if difference else 0.0,
    }
