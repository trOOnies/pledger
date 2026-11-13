"""movements.py: the rows of the movements of the ledger CSV, their checks, and the initial ones."""

from __future__ import annotations

import datetime as dt
import math
from typing import TYPE_CHECKING

from pandas import DataFrame

from pledger.enums import AccountType, CreditorKind, DebtRole, MovementType
from pledger.models import (
    Account,
    CreditCardAccount,
    DebtAccount,
    Movement,
    MovementBase,
)
from pledger.web.store.common import (
    _check_free_sides,
    _generated_rows,
    _parse_amount,
    _quantity_text,
    _required_base,
    _value_on,
    own_initial_amount,
)
from pledger.web.store.constants import (
    CART_COLUMN,
    ID_COLUMN,
    INITIAL_GAIN,
    INITIAL_ID,
    INITIAL_LOSS,
    INSTALLMENTS_COLUMN,
    INTEREST,
    INTEREST_ACCOUNT,
    INVESTMENT_COLUMN,
    MAX_INSTALLMENTS,
    OTHER_AMOUNT,
    QUANTITY_COLUMN,
)
from pledger.web.store.investments import _check_invest, _investment_details

if TYPE_CHECKING:
    from pledger.web.store.snapshot import Snapshot


def _parse_installments(text: str | int, what: str) -> int:
    """A number of installments: a whole number from 1 to MAX_INSTALLMENTS, 1 if empty."""
    text = str(text).strip()
    if not text:
        return 1
    if not text.isdigit() or not 1 <= int(text) <= MAX_INSTALLMENTS:
        raise ValueError(f"The {what} must be a whole number from 1 to {MAX_INSTALLMENTS}: {text!r}")
    return int(text)


def _installments_text(snapshot: Snapshot, installments: str | int, acc_credit: str) -> str:
    """
    The INSTALLMENTS_COLUMN of a new ledger row: empty for 1, else the number, which only
    a charge to a credit card (crediting it) can have.
    """
    n = _parse_installments(installments, "number of installments")
    if n > 1 and not isinstance(snapshot.accounts.get(acc_credit), CreditCardAccount):
        raise ValueError("Only a movement crediting a credit card can be in installments")
    return str(n) if n > 1 else ""


def installments_of(snapshot: Snapshot, row) -> int:
    """
    The number of installments of a row of the ledger (see INSTALLMENTS_COLUMN): 1 without
    any. Raises ValueError if it is invalid, or more than 1 not crediting a credit card.
    """
    value = row.get(INSTALLMENTS_COLUMN, "")
    text = "" if value is None or (isinstance(value, float) and math.isnan(value)) else str(value)
    n = _parse_installments(text, f"{INSTALLMENTS_COLUMN} of the movement of {row['move_date']}")
    if n > 1 and not isinstance(snapshot.accounts.get(row["acc_credit"]), CreditCardAccount):
        raise ValueError(f"Only a movement crediting a credit card can be in installments ({row['move_date']})")
    return n


def cart_of(row) -> str | None:
    """The id of the cart a row of the ledger CSV is a product of (see CART_COLUMN), None if of no cart."""
    value = row.get(CART_COLUMN, "")
    text = "" if value is None or (isinstance(value, float) and math.isnan(value)) else str(value).strip()
    return text.split("/")[0].strip() or None if text else None


def _check_not_cart(row, action: str) -> None:
    """The movements of a cart are only edited and deleted from the Cart tab (see CartStore)."""
    cart = cart_of(row)
    if cart is not None:
        raise ValueError(f"The movement is a product of the cart {cart}: it is {action} from the Cart tab")


# Each kind of creditor, as a noun, and the tab of its debt accounts
KIND_ARTICLES = {CreditorKind.PERSON: "a person", CreditorKind.SERVICE: "a service", CreditorKind.COMPANY: "a company"}
KIND_TABS = {CreditorKind.PERSON: "People", CreditorKind.SERVICE: "Services", CreditorKind.COMPANY: "Work"}


def _debt_comment(snapshot: Snapshot, acc_debit: str, acc_credit: str, who: str, comment: str) -> str:
    """
    The comment of a debt movement about `who` (see LedgerStore.add_debt_movement), naming
    them first. They must be someone of its debt account: a person in the debt accounts
    of the people, a service in SeD or SeL, a company in Wo.
    """
    accounts = [snapshot.accounts.get(n) for n in (acc_debit, acc_credit)]
    debts = [a for a in accounts if isinstance(a, DebtAccount)]
    who, comment = who.strip(), comment.replace(";", ",").strip()
    if not who:
        raise ValueError("Pick who the debt movement is about")
    found = [a for a in snapshot.accounts.values() if isinstance(a, DebtAccount) and who in a.creditors]
    if debts and found and not any(a.kind == found[0].kind for a in debts):
        kind = found[0].kind
        where = "the debt accounts of the people" if kind == CreditorKind.PERSON else " and ".join(a.name for a in found)
        raise ValueError(
            f"{who!r} is {KIND_ARTICLES[kind]}: their movements are in {where} ({KIND_TABS[kind]} tab), "
            f"not in {debts[0].name}"
        )
    if debts and not any(who in a.creditors for a in debts):
        if found:
            expected = found[0].creditors[who].expected
            raise ValueError(
                f"{who!r} is expected to be a {expected.value}: their movements are in the "
                f"{'debt' if expected == DebtRole.CREDITOR else 'loan'} accounts, not in {debts[0].name}"
            )
        raise ValueError(f"{who!r} is not someone of the debt account {debts[0].name}")
    if not debts or debts[0].creditor_in(comment) != who:
        comment = f"{who}: {comment}" if comment else who
    return comment


def _movement_row(
    snapshot: Snapshot,
    move_date: str,
    base_movement: str,
    acc_debit: str,
    acc_credit: str,
    amount: str | float,
    comment: str,
    invest: bool,
    investment: str,
    quantity: str | float,
    debt: bool,
    card_payment: bool,
    correction: bool = False,
    difference: bool = False,
) -> tuple[str, dict]:
    """
    Validate a movement (see LedgerStore.add) and give the Snapshot field of the CSV
    it goes to ("rows", "interest" or "investments") with its row there.
    """
    try:
        dt.date.fromisoformat(move_date)
    except ValueError:
        raise ValueError(f"Invalid date: {move_date!r}") from None
    if base_movement not in snapshot.base_movements:
        raise ValueError(f"Unknown base movement: {base_movement!r}")
    _check_invest(snapshot, base_movement, invest, "added")
    _check_tab(snapshot, base_movement, invest, debt, card_payment, correction=correction, difference=difference)
    if not (invest or debt or card_payment or correction or difference):
        tab = snapshot.auto_base_movements().get(base_movement)
        if tab is not None:
            raise ValueError(f"The movements of the base movement {base_movement!r} are created from the {tab} tab")
    has_details = str(investment).strip() or str(quantity).strip()
    if has_details and (not invest or base_movement in INTEREST):
        raise ValueError("Only movements of the investments have an investment and a quantity")
    for name in (acc_debit, acc_credit):
        if name not in snapshot.accounts:
            raise ValueError(f"Unknown account: {name!r}")
    value = _parse_amount(
        amount, "amount",
        "The amount cannot be negative; pick the opposite base movement or swap the accounts",
    )
    if acc_debit == acc_credit:
        raise ValueError("Debit and credit accounts must be different")

    base = snapshot.base_movements[base_movement]
    try:
        Movement(
            move_date=move_date,
            move_base=base,
            input_acc_debit=snapshot.accounts[acc_debit],
            input_acc_credit=snapshot.accounts[acc_credit],
            amount=value,
        )
    except AssertionError as e:
        raise ValueError(str(e)) from None
    if invest:
        _check_free_sides(base, snapshot.accounts[acc_debit], snapshot.accounts[acc_credit])

    comment = comment.replace(";", ",").strip()
    new_row = {
        "move_date": move_date,
        "base_movement": base_movement,
        "acc_debit": acc_debit,
        "acc_credit": acc_credit,
        "amount": f"{value:,.2f}",
        "comment": comment,
    }
    if not invest:
        field = "rows"
    elif base_movement in INTEREST:
        field = "interest"
        gain = base.move_type == MovementType.GAIN
        new_row = {
            "move_date": move_date,
            INTEREST_ACCOUNT: acc_debit if gain else acc_credit,
            "amount": f"{'' if gain else '-'}{value:,.2f}",
            "comment": comment,
        }
    else:
        field = "investments"
        name, units = _investment_details(base, investment, quantity)
        new_row |= {INVESTMENT_COLUMN: name, QUANTITY_COLUMN: _quantity_text(units) if units else ""}
    return field, new_row


def _check_tab(
    snapshot: Snapshot, base_name: str, invest: bool, debt: bool, card_payment: bool, action: str = "added",
    correction: bool = False, difference: bool = False,
) -> None:
    """
    Each tab adds (or edits, or deletes: the `action`) the movements of its own base
    movements: the conversions are only added from the Currencies tab, the debt
    movements from the People tab, the card payments from the Credit Cards tab, the
    balance corrections from the Audit tab (`correction`; the Ledger tab deletes
    them with it too), the differences with their debt movement (`difference`; the Ledger and Debt
    tabs delete them with it too), and
    all the others from the Ledger tab (or the Investments tab,
    see _check_invest).
    """
    if base_name in snapshot.conversion_base_names:
        raise ValueError(f"The conversions ({base_name!r}) are added from the Currencies tab")
    for wanted, bases, what, tab in (
        (debt, snapshot.debt_base_movements(), "debt", "People, Services or Work"),
        (card_payment, snapshot.card_payment_base_movements(), "card payment", "Credit Cards"),
        (correction, snapshot.correction_base_movements(), "balance correction", "Audit"),
        (difference, snapshot.difference_base_movements(), "debt difference", "People"),
    ):
        if wanted and base_name not in bases:
            raise ValueError(f"Base movement {base_name!r} is not a {what} base movement")
        if base_name in bases and not wanted:
            raise ValueError(f"Movements of the base movement {base_name!r} are {action} from the {tab} tab")
    if invest and (debt or card_payment):
        raise ValueError("A movement of the investments is not a debt movement or a card payment")


def _check_initial_base_movements(base_movements: dict[str, MovementBase]) -> None:
    """INITIAL_GAIN and INITIAL_LOSS record the initial amount of the accounts (see _required_base)."""
    _required_base(base_movements, INITIAL_GAIN, gain=True)
    _required_base(base_movements, INITIAL_LOSS, gain=False)


def _initial_rows(snapshot: Snapshot, first_date: str) -> DataFrame:
    """
    The movement recording the initial amount of each account, in definition order,
    as CSV rows: on its initial date, else the first date of the ledger. Increases are
    INITIAL_GAIN and decreases INITIAL_LOSS movements against the equity account. The
    initial amount of an account in another currency is in that currency (see
    own_initial_amount): its movement is at the value of the currency on that date.
    """
    base_movements, columns = snapshot.base_movements, snapshot.rows.columns
    new_rows = []
    for acc in snapshot.accounts.values():
        amount, comment = acc.initial_amount, ""
        if acc.acc_type == AccountType.EQUITY or math.isclose(amount, 0.0, abs_tol=1e-9):
            continue
        move_date = acc.initial_date.isoformat() if acc.initial_date else first_date
        if own_initial_amount(acc):
            value = _value_on(acc.currency, move_date, f"to set the initial amount of {acc.name!r}")
            comment = f"{amount:,.4f} {acc.currency.name} at {value:,.4f}"
            amount = round(amount * value, 2)
        # A positive liability is a decrease of equity, like a negative asset.
        if (amount > 0) == (acc.acc_type == AccountType.ASSET):
            base = base_movements[INITIAL_GAIN]
            acc_debit, acc_credit = acc.name, base.acc_credit.name
        else:
            base = base_movements[INITIAL_LOSS]
            acc_debit, acc_credit = base.acc_debit.name, acc.name
        new_rows.append({
            "move_date": move_date,
            "base_movement": base.name,
            "acc_debit": acc_debit,
            "acc_credit": acc_credit,
            "amount": repr(abs(amount)),
            ID_COLUMN: f"{INITIAL_ID}-{acc.name}",
            **({"comment": comment} if comment and "comment" in columns else {}),
        })
    return _generated_rows(new_rows, columns, -1)


def debt_amount(snapshot: Snapshot, account: Account, row) -> float:
    """
    The amount of a movement in the currency of the debt account on a side: its OTHER_AMOUNT
    if it is in another currency and has one (see _other_amount), else its amount, at the
    value of the currency on its date for one in another currency (the last one up to then).
    """
    amount = float(str(row["amount"]).replace(",", "").replace(" ", "") or 0)
    if account.currency.is_base_currency:
        return amount
    other = str(row.get(OTHER_AMOUNT, "") or "").strip()
    if other:
        return float(other.replace(",", "").replace(" ", ""))
    return amount / _value_on(account.currency, row["move_date"], f"to follow {account.name!r} in its currency")


def _apply_debts(snapshot: Snapshot, accounts: dict[str, Account], rows: DataFrame) -> None:
    """
    Add the CSV movements of the debt accounts to whoever is named in their comments, in
    the currency of the account (see debt_amount): a credit is more that we owe them, a
    debit less. The initial movements are left out, as the initial amounts already come
    from the creditors YAML, and so are the revaluations of the accounts in other
    currencies, which change their value in the base currency, not what is owed in theirs.
    """
    for row in rows.to_dict("records"):
        for name, sign in ((row["acc_debit"], -1), (row["acc_credit"], 1)):
            acc = accounts[name]
            if isinstance(acc, DebtAccount):
                comment = row.get("comment", "") or ""
                acc.add_debt(acc.creditor_in(comment), sign * debt_amount(snapshot, acc, row), comment)
