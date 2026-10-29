"""debt.py: the Debt tab: who we owe and who owes us, and the debt movement form."""

from __future__ import annotations

from pledger.enums import CreditorKind
from pledger.models import DEBT_ACCOUNT, DebtAccount, rounds_to_zero
from pledger.web.store import (
    DEBT_BASES,
    DEBT_INCURRED,
    DEBT_PAID,
    LOAN_COLLECTED,
    LOAN_GIVEN,
    Snapshot,
)
from pledger.web.views.common import account_movements, grouped_composition, months_of
from pledger.web.views.forms import account_fields

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
    bases, missing = snapshot.tab_base_movements(DEBT_BASES, DEBT_ACCOUNT)
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
