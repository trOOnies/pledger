"""split_store.py: the movements of the user's part of a split (the Split tab), and the people it adds."""

from __future__ import annotations

from pledger.enums import CreditorKind, DebtRole
from pledger.models import DebtAccount
from pledger.web.store.base_store import StoreBase
from pledger.web.store.cart_store import cart_accounts, cart_bases
from pledger.web.store.common import LedgerDataError, _atomic_write, _parse_number
from pledger.web.store.constants import DEBT_PAID, LOAN_COLLECTED, LOAN_GIVEN
from pledger.web.store.movements import _debt_comment, _movement_row
from pledger.web.store.snapshot import Snapshot

SPLIT_COMMENT = "Split"  # the comment of the debt movements of a split without a description


def people_of(snapshot: Snapshot) -> tuple[dict[str, str], dict[str, CreditorKind]]:
    """The people of the debt accounts by their name ignoring case, and the others (services, companies) with their kind."""
    people, others = {}, {}
    for account in snapshot.accounts.values():
        if isinstance(account, DebtAccount):
            for name, c in account.creditors.items():
                if c.kind == CreditorKind.PERSON:
                    people[name.casefold()] = name
                else:
                    others[name.casefold()] = c.kind
    return people, others


class SplitStore(StoreBase):
    """Plans and creates the movements of the user's part of a split, adding the people it needs."""

    def plan_split(
        self, result: dict, move_date: str, description: str = "", paid_from: str = "",
        settle: dict[str, tuple[str, str]] | None = None,
    ) -> dict:
        """
        The movements of the user's part of a split (views.split.split, the user first), checked
        but not written, and the `new_people` they need, missing from the People tab. P is what
        the user put in, C their share of the items:
        - Each item they have a share in is a movement of its `base` (a loss of the Ledger tab), of
          their share. If P >= C, out of `paid_from`; else out of it up to P, then out of the debt
          account of the people for what each one they owe paid for them (an item can be in two).
        - If P > C, a Loan - Given out of `paid_from` to each one who owes them.
        - `settle`, by who: what is settled now with each one in a transfer with the user, and the
          account it goes into (Loan - Collected) or out of (Debt - Paid); the rest stays owed.
        Each movement is a dict of its fields, `who` it is about (if any) and its ledger `row`.
        """
        from pledger.web.views.split import (  # the views import the store
            YOU,
            settling_rows,
        )

        snapshot = self.load()
        settle = settle or {}
        you = result["people"][0]
        paid, share = you["paid"], you["owes"]
        description = description.replace(";", ",").strip()
        accounts = cart_accounts(snapshot)
        known, others = people_of(snapshot)

        def account_of(name: str, what: str) -> str:
            name = name.strip()
            if not name:
                raise ValueError(f"Pick the account {what}")
            if name not in accounts:
                raise ValueError(f"{name!r} can't be the account {what}: pick one in {snapshot.base_currency.name} of yours")
            return name

        # Who the user has a transfer with, as in the People tab if they are there
        settling, new_people = [], []
        for row in settling_rows(result):
            key = row["who"].casefold()
            if key in others:
                raise ValueError(
                    f"{row['who']!r} is a {'service' if others[key] == CreditorKind.SERVICE else 'company'} "
                    "of pledger, not a person: rename them in the split"
                )
            who = known.get(key)
            if who is None:
                who = row["who"]
                new_people.append(who)
            settling.append({**row, "who": who, "name": row["who"]})
        debt = loan = None
        if settling:
            debt, loan = (snapshot.base_debt_account(r) for r in (DebtRole.CREDITOR, DebtRole.DEBTOR))
            if debt is None or loan is None:
                raise ValueError(f"Settling needs the debt and loan accounts of the people in {snapshot.base_currency.name}")
            missing = [b for b in (DEBT_PAID, LOAN_GIVEN, LOAN_COLLECTED) if b not in snapshot.debt_base_movements()]
            if missing:
                raise ValueError(f"Settling needs these base movements of the People tab: {', '.join(missing)}")
        source = account_of(paid_from, "you paid from") if paid else ""

        movements = []

        def add(base: str, debit: str, credit: str, cents: int, comment: str, who: str | None = None, debt: bool = False):
            movements.append({
                "base_movement": base, "acc_debit": debit, "acc_credit": credit, "cents": cents,
                "comment": comment, "who": who, "debt": debt,
            })

        # The user's share of each item, out of what they paid, then out of who paid for them
        sources = [(source, None, min(paid, share))] if paid else []
        sources += [(debt.name, r["who"], r["cents"]) for r in settling if not r["owes_you"]]
        bases = cart_bases(snapshot)
        for item in result["items"]:
            left = item["shares"][0]
            if not left:
                continue
            base = item.get("base", "")
            if not base:
                raise ValueError(f"Pick the base movement of {item['name']!r}: you have a share in it")
            if base not in bases:
                raise ValueError(f"{base!r} can't be the base movement of {item['name']!r}: pick a loss one")
            comment = f"{description} - {item['name']}" if description else item["name"]
            while left:
                account, who, available = sources[0]
                cents = min(left, available)
                add(base, snapshot.base_movements[base].acc_debit.name, account, cents, comment, who)
                left -= cents
                sources[0] = (account, who, available - cents)
                if not sources[0][2]:
                    sources.pop(0)
        debt_comment = description or SPLIT_COMMENT
        for r in settling:
            if r["owes_you"]:  # what the user paid for them
                add(LOAN_GIVEN, loan.name, source, r["cents"], debt_comment, r["who"], debt=True)
        for r in settling:
            amount, account = settle.get(r["name"], settle.get(r["who"], ("", "")))
            cents = round(_parse_number(amount, f"amount settled with {r['who']}") * 100) if str(amount).strip() else 0
            if cents < 0:
                raise ValueError(f"The amount settled with {r['who']} cannot be negative")
            if cents > r["cents"]:
                owed = "they owe you" if r["owes_you"] else "you owe " + r["who"]
                raise ValueError(f"{r['who'] if r['owes_you'] else 'You'} can't settle more than the {r['cents'] / 100:,.2f} {owed}")
            if not cents:
                continue
            if r["owes_you"]:
                into = account_of(account, f"{r['who']} pays you into")
                add(LOAN_COLLECTED, into, loan.name, cents, debt_comment, r["who"], debt=True)
            else:
                out = account_of(account, f"you pay {r['who']} from")
                add(DEBT_PAID, debt.name, out, cents, debt_comment, r["who"], debt=True)

        # Their ledger rows, checked together (those about new people once they are added)
        for m in movements:
            comment = m["comment"]
            if m["who"] is not None:
                comment = (
                    f"{m['who']}: {comment}" if m["who"] in new_people
                    else _debt_comment(snapshot, m["acc_debit"], m["acc_credit"], m["who"], comment)
                )
            _, m["row"] = _movement_row(
                snapshot, move_date, m["base_movement"], m["acc_debit"], m["acc_credit"], m["cents"] / 100, comment,
                False, "", "", m["debt"], False,
            )
            m["comment"] = comment
        if movements:
            try:
                self._check_rows(snapshot, "rows", [m["row"] for m in movements])
            except LedgerDataError as e:
                raise ValueError(str(e)) from None
        return {"movements": movements, "new_people": new_people, "you": YOU}

    def create_split(
        self, result: dict, move_date: str, description: str = "", paid_from: str = "",
        settle: dict[str, tuple[str, str]] | None = None,
    ) -> dict:
        """
        Add the new people of a split to the People tab and its movements (see plan_split) to the
        ledger CSV, all together: if they don't make it, the creditors YAML is as it was.
        """
        plan = self.plan_split(result, move_date, description, paid_from, settle)
        if not plan["movements"]:
            raise ValueError("There are no movements to create")
        path = self.settings.creditors_path
        before = path.read_bytes() if path is not None and path.exists() else None
        added = plan["new_people"]
        try:
            for name in added:
                self.add_person(name)
            plan = self.plan_split(result, move_date, description, paid_from, settle)
            self._append_rows(self.load(), "rows", [m["row"] for m in plan["movements"]])
        except (LedgerDataError, ValueError):
            if path is not None and added:
                if before is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, before.decode("utf-8"))
            raise
        return {**plan, "new_people": added}
