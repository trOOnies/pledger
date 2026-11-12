"""linked_store.py: the movements of several rows: broker fees, and paying or being paid in other currencies."""

from __future__ import annotations

from dataclasses import replace

from pandas import DataFrame, concat, read_csv

from pledger.enums import AccountType, LedgerSide, MovementType
from pledger.models import (
    INTERMEDIARY,
    Account,
    DebtAccount,
    InvestmentAccount,
    check_interact,
    is_foreign,
)
from pledger.web.store.common import (
    _amount_of,
    _fit_row,
    _parse_amount,
    _parse_date,
    _value_on,
    stored_ids,
)
from pledger.web.store.constants import (
    BROKER_FEES,
    CONVERSION_BOUGHT,
    CONVERSION_COLUMNS,
    CONVERSION_ID,
    CONVERSION_SOLD,
    DIFFERENCE_GAIN,
    FEE_BASES,
    INTEREST,
    INVESTMENT_COLUMN,
    LEDGER_ID,
    LINKED_COLUMN,
    OTHER_AMOUNT,
)
from pledger.web.store.conversions import _conversion_row, linked_ids
from pledger.web.store.investments import _check_known_investment
from pledger.web.store.movement_store import MovementStore, foreign_card
from pledger.web.store.movements import _debt_comment, _installments_text, _movement_row
from pledger.web.store.snapshot import Snapshot

NEGATIVE = "The amount cannot be negative; pick the opposite base movement or swap the accounts"


def _money_side(snapshot: Snapshot, acc_debit: str, acc_credit: str) -> str | None:
    """Which side of a movement the money comes or goes through ("debit" or "credit"): its only regular account."""
    regular = [
        side for side, name in (("debit", acc_debit), ("credit", acc_credit))
        if name in snapshot.accounts and snapshot.accounts[name].acc_type != AccountType.EQUITY
        and not isinstance(snapshot.accounts[name], InvestmentAccount)
    ]
    return regular[0] if len(regular) == 1 else None


def _fi_leg(
    snapshot: Snapshot, move_date: str, account: Account, inflow: bool, base_amount: float, amount: float, comment: str,
) -> dict:
    """
    The conversions CSV row moving the money of a movement between an account in another
    currency and the INTERMEDIARY one: `amount` in its currency, `base_amount` in the base one.
    """
    if inflow:
        args = (CONVERSION_BOUGHT, account.name, INTERMEDIARY)
    else:
        args = (CONVERSION_SOLD, INTERMEDIARY, account.name)
    return _conversion_row(snapshot, move_date, *args, f"{base_amount:.2f}", f"{amount:.6f}", comment)


def foreign_result_account(snapshot: Snapshot, base_movement: str, acc_debit: str, acc_credit: str) -> Account | None:
    """
    The account in another currency of a gain or loss of the Ledger tab, if any: its
    amount is in the currency of that account (see LinkedStore.add_ledger_movement).
    """
    base = snapshot.base_movements.get(base_movement)
    side = _money_side(snapshot, acc_debit, acc_credit)
    if base is None or base.move_type == MovementType.TRANSFER or side is None:
        return None
    account = snapshot.accounts[acc_debit if side == "debit" else acc_credit]
    return account if is_foreign(account) else None


def _check_installments(snapshot: Snapshot, installments: str | int, acc_credit: str, account: Account) -> None:
    """A movement of an account in another currency through the Ledger tab can't be in installments."""
    if _installments_text(snapshot, installments, acc_credit):
        raise ValueError(f"A charge to a card in another currency ({account.name}) can't be in installments")


def _own_amount(own: Account, other: Account, value: float, amount_own: str | float, what: str) -> float:
    """
    The amount of a movement in the currency of `own`, a credit card or debt account,
    paid with `value` in that of the `other` account, which may already say it.
    """
    same = other.currency.name == own.currency.name
    if not str(amount_own).strip():
        if not same:
            raise ValueError(f"{own.name} is in {own.currency.name}: tell how much {what} in {own.currency.name}")
        return value
    paid = _parse_amount(amount_own, f"amount in {own.currency.name}")
    if same and abs(paid - value) > 1e-9:
        raise ValueError(f"{other.name} and {own.name} are both in {own.currency.name}: the amounts must match")
    if paid == 0:
        raise ValueError(f"The amount in {own.currency.name} must be positive")
    return paid


class LinkedStore(MovementStore):
    """Adds the movements that take several rows, through the INTERMEDIARY account if they change currency."""

    def add_ledger_movement(
        self, move_date: str, base_movement: str, acc_debit: str, acc_credit: str, amount: str | float, comment: str = "",
        installments: str | int = "",
    ) -> None:
        """
        Add a movement of the Ledger tab (see add), a charge to a credit card possibly in
        `installments`. The `amount` of a gain or loss of an
        account in another currency is in that currency: at its value on that date, the
        movement goes through the INTERMEDIARY account, in the base currency, with a
        conversion linked to it, and can't be in installments. A base movement with that account fixed can't: it is
        a single row, of the amount in the base currency. Transfers are in the base one.
        """
        snapshot = self.load()
        account = foreign_result_account(snapshot, base_movement, acc_debit, acc_credit)
        if account is None:
            self.add(move_date, base_movement, acc_debit, acc_credit, amount, comment, installments=installments)
            return
        if _parse_date(move_date, "date") is None:
            raise ValueError("The date is required")
        value = _parse_amount(amount, "amount", NEGATIVE)
        base = snapshot.base_movements[base_movement]
        rate = _value_on(account.currency, move_date, f"to convert from {account.currency.name}")
        base_value = round(value * rate, 2)
        if isinstance(account, DebtAccount):
            # Converted on its date: a single row, the debt account following its own amount
            _check_installments(snapshot, installments, acc_credit, account)
            field, row = _movement_row(
                snapshot, move_date, base_movement, acc_debit, acc_credit, base_value, comment, False, "", "", False, False,
            )
            self._add_linked(snapshot, [(field, row | {OTHER_AMOUNT: f"{value:,.4f}"})], None)
            return
        inflow = account.name == acc_debit
        if base.side(LedgerSide.DEBIT if inflow else LedgerSide.CREDIT) is not None:
            self.add(move_date, base_movement, acc_debit, acc_credit, f"{base_value:.2f}", comment, installments=installments)
            return
        _check_installments(snapshot, installments, acc_credit, account)
        debit, credit = (INTERMEDIARY, acc_credit) if inflow else (acc_debit, INTERMEDIARY)
        row = _movement_row(snapshot, move_date, base_movement, debit, credit, base_value, comment, False, "", "", False, False)
        self._add_linked(snapshot, [row], _fi_leg(snapshot, move_date, account, inflow, base_value, value, comment))

    def add_investment_movement(
        self,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount: str | float,
        comment: str = "",
        investment: str = "",
        quantity: str | float = "",
        currency: str = "",
        fees: str | float = "",
    ) -> None:
        """
        Add a movement of the Investments tab (see add), with its `amount` and `fees` in
        `currency` (the base one by default), at its value on that date.

        `fees` adds a BROKER_FEES movement of the investment, out of the same account, to a
        movement of FEE_BASES. In another currency, and with an account of another currency
        as the side of the money (see _money_side), the movements go through the
        INTERMEDIARY account, and a conversion links it to that account for what is left
        after the fees (more with them, if the money goes out). The rows of the interest on
        money stay in their account: their conversion to the base one is just arithmetic.
        """
        snapshot = self.load()
        name = currency.strip() or snapshot.base_currency.name
        if name not in snapshot.currencies:
            raise ValueError(f"Unknown currency: {name!r}")
        if _parse_date(move_date, "date") is None:
            raise ValueError("The date is required")
        value = _parse_amount(amount, "amount", NEGATIVE)
        fee = _parse_amount(fees, "broker fees") if str(fees).strip() else 0.0
        if fee and base_movement not in FEE_BASES:
            raise ValueError(f"Movements of {base_movement!r} can't add broker fees: add them as their own movement")
        rate = _value_on(snapshot.currencies[name], move_date, f"to convert from {name}")
        base_value, base_fee = round(value * rate, 2), round(fee * rate, 2)

        side = _money_side(snapshot, acc_debit, acc_credit)
        money = snapshot.accounts[acc_debit if side == "debit" else acc_credit] if side else None
        routed = (
            money is not None and is_foreign(money) and name != snapshot.base_currency.name
            and base_movement not in INTEREST and not isinstance(money, DebtAccount)
        )
        debit, credit = acc_debit, acc_credit
        if routed:
            # The account picked, not the INTERMEDIARY one that takes its place in the rows
            if debit in snapshot.accounts and credit in snapshot.accounts:
                check_interact(snapshot.accounts[debit], snapshot.accounts[credit], move_date)
            debit, credit = (INTERMEDIARY, credit) if side == "debit" else (debit, INTERMEDIARY)
        if not routed and not fee:
            self.add(
                move_date, base_movement, debit, credit, f"{base_value:.2f}", comment,
                invest=True, investment=investment, quantity=quantity,
            )
            return
        rows = [_movement_row(
            snapshot, move_date, base_movement, debit, credit, base_value, comment,
            True, investment, quantity, False, False,
        )]
        if fee:
            fees_base = snapshot.invest_base_movements().get(BROKER_FEES)
            if fees_base is None or side is None:
                raise ValueError(f"Broker fees need the {BROKER_FEES!r} base movement and an account to come out of")
            rows.append(_movement_row(
                snapshot, move_date, BROKER_FEES, fees_base.acc_debit.name,
                debit if side == "debit" else credit, base_fee, comment, True, investment, "", False, False,
            ))
        conversion = None
        if routed:
            inflow = side == "debit"
            net_base = base_value - base_fee if inflow else base_value + base_fee
            if net_base <= 0:
                raise ValueError("The broker fees can't take up the whole amount of the movement")
            own = value - fee if inflow else value + fee
            net = own if name == money.currency.name else net_base / _value_on(money.currency, move_date, "to convert")
            conversion = _fi_leg(snapshot, move_date, money, inflow, round(net_base, 2), net, comment)
        if rows[0][0] == "investments":
            _check_known_investment(snapshot, acc_debit, acc_credit, rows[0][1][INVESTMENT_COLUMN])
        self._add_linked(snapshot, rows, conversion)

    def _add_linked(
        self, snapshot: Snapshot, rows: list[tuple[str, dict]], conversion: dict | None, linked: int | None = None,
    ) -> None:
        """
        Append these rows ((Snapshot field, row)) to their CSVs, and this conversion with
        their ids in its LINKED_COLUMN (only those of the first `linked` ones, if given),
        once they hold together.
        """
        new_rows: dict[str, list[dict]] = {}
        for field, row in rows:
            new_rows.setdefault(field, []).append(row)
        changes = {
            field: concat([getattr(snapshot, field), DataFrame(found)], ignore_index=True).fillna("")
            for field, found in new_rows.items()
        }
        if conversion is not None:
            self._movement_file("conversions")
            changes["conversions"] = concat([snapshot.conversions, DataFrame([conversion])], ignore_index=True).fillna("")
        changed = replace(snapshot, **changes)
        changed.ledger().check_accounts()
        changed.investment_accounts()  # e.g. not selling more than is held
        ids = []
        for field, found in new_rows.items():
            path, what, prefix = self._movement_file(field)
            frame = getattr(snapshot, field)
            extra = [c for row in found for c in row if c not in frame.columns and c == OTHER_AMOUNT]
            for column in set(extra):
                self._add_column(path, len(frame), column)
            fitted = [_fit_row(row, [*frame.columns, *extra], what) for row in found]
            self._write_file(self._appended(path, prefix, fitted, list(frame.columns)))
            written = read_csv(path, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
            ids += stored_ids(written, prefix)[-len(found):]
        if conversion is not None:
            path = self._movement_file("conversions")[0]
            self._add_column(path, len(snapshot.conversions), LINKED_COLUMN)
            row = conversion | {LINKED_COLUMN: " ".join(ids[:linked])}
            self._write_file(self._appended(path, CONVERSION_ID, row, [*CONVERSION_COLUMNS, LINKED_COLUMN]))

    def add_card_payment(
        self,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount: str | float,
        comment: str = "",
        amount_card: str | float = "",
    ) -> None:
        """
        Add a card payment or surplus. For a card in another currency, `amount` is in the
        currency of the other account and `amount_card` is what was paid in that of the card,
        only optional if they are the same: the card follows that amount exactly, kept in the
        OTHER_AMOUNT of its row. An account in another currency goes through the INTERMEDIARY
        account, at the value of its currency on that date, with a conversion linked to it.
        """
        snapshot = self.load()
        card = foreign_card(snapshot, acc_debit, acc_credit)
        if card is None:
            if str(amount_card).strip():
                raise ValueError("Only a card in another currency has an amount in the card's currency")
            self.add(move_date, base_movement, acc_debit, acc_credit, amount, comment, card_payment=True)
            return
        if _parse_date(move_date, "date") is None:
            raise ValueError("The date is required")
        other = snapshot.accounts.get(acc_credit if card.name == acc_debit else acc_debit)
        if other is None:
            raise ValueError(f"Unknown account: {acc_credit if card.name == acc_debit else acc_debit!r}")
        if isinstance(other, DebtAccount) and is_foreign(other):
            raise ValueError(
                f"A card in another currency can't be paid with the debt account {other.name}, in another "
                "currency too: pay it from an account, then add the debt in the Debt tab"
            )
        value = _parse_amount(amount, "amount", NEGATIVE)
        paid = _own_amount(card, other, value, amount_card, "debt was paid")
        rate = _value_on(other.currency, move_date, f"to convert from {other.currency.name}")
        base_value = round(value * rate, 2)
        through = INTERMEDIARY if is_foreign(other) else other.name
        debit, credit = (card.name, through) if card.name == acc_debit else (through, card.name)
        field, row = _movement_row(
            snapshot, move_date, base_movement, debit, credit, base_value, comment, False, "", "", False, True,
        )
        row[OTHER_AMOUNT] = f"{paid:,.4f}"
        conversion = None
        if is_foreign(other):
            conversion = _fi_leg(snapshot, move_date, other, card.name == acc_credit, base_value, value, comment)
        self._add_linked(snapshot, [(field, row)], conversion)

    def edit_card_payment(
        self,
        index: int,
        fingerprint: str | None,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount: str | float,
        comment: str = "",
        amount_card: str | float = "",
    ) -> None:
        """
        Replace a card payment of the ledger CSV by position (see edit), with its `amount`
        in the base currency, as it is in the CSV, and for a card in another currency
        its `amount_card`, required there. One added with a conversion keeps its amount.
        """
        snapshot = self.load()
        card = foreign_card(snapshot, acc_debit, acc_credit)
        if card is None:
            if str(amount_card).strip():
                raise ValueError("Only a card in another currency has an amount in the card's currency")
            self.edit(index, fingerprint, move_date, base_movement, acc_debit, acc_credit, amount, comment, card_payment=True)
            return
        if not str(amount_card).strip():
            raise ValueError(f"{card.name} is in {card.currency.name}: tell how much debt was paid in {card.currency.name}")
        paid = _parse_amount(amount_card, f"amount in {card.currency.name}")
        current = self.current_row(snapshot, "rows", index, fingerprint)
        own = stored_ids(snapshot.rows, LEDGER_ID)[index]
        if own in linked_ids(snapshot) and _parse_amount(amount, "amount") != _amount_of(current["amount"]):
            raise ValueError("This amount was added with its conversion: delete the payment and add it again")
        self._add_column(self.settings.ledger_path, len(snapshot.rows), OTHER_AMOUNT)
        self.edit(
            index, fingerprint, move_date, base_movement, acc_debit, acc_credit, amount, comment,
            card_payment=True, amount_other=f"{paid:,.4f}",
        )

    def add_debt_movement(
        self,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount: str | float,
        who: str,
        comment: str = "",
        remaining_as_difference: bool = False,
        amount_debt: str | float = "",
    ) -> tuple[str, float] | None:
        """
        Add a movement of a debt base movement to the ledger CSV, about `who`, someone of
        its debt account but a company (their debt comes from the work history): its
        comment names them first (prefixed if it doesn't), so the Debt tab adds it to them.

        Its debt account, of any currency, follows `amount_debt`, in its currency, and the
        other account `amount`, in its own: only optional if they are the same (see
        _own_amount). So a movement between two currencies was converted when it was made:
        - With both accounts in the base currency, it is a single row, as usual.
        - With a debt account in another currency and the other account in the base one, it
          is a single row of `amount`, with `amount_debt` in its OTHER_AMOUNT.
        - With the other account in another currency, it goes through the INTERMEDIARY
          account, with a conversion linked to it for `amount`: the row is of `amount_debt`
          for a debt account in the base currency, else of `amount` at the value of its
          currency on that date, with `amount_debt` in its OTHER_AMOUNT.
        What it was worth over or under the value of the currencies then is a result of
        the next revaluation of each account in another currency (see _result_rows).

        With `remaining_as_difference`, what would remain with them after it, in all the
        ledger, in its debt account and currency, is settled there by a DIFFERENCES movement
        of the same date and comment, against the equity account, at the value of its
        currency on that date, added together with it (see Snapshot.difference_of). Returns
        its base movement and amount in that currency, None if nothing remains or it wasn't
        asked for.
        """
        snapshot = self.load()
        comment = _debt_comment(snapshot, acc_debit, acc_credit, who, comment)
        debt = snapshot.debt_account_of(base_movement, acc_debit, acc_credit)
        other = snapshot.accounts.get(acc_credit if debt is not None and debt.name == acc_debit else acc_debit)
        if debt is None or other is None or (debt.currency.is_base_currency and other.currency.is_base_currency):
            # One amount, in the base currency (an unknown account or base movement fails below)
            if str(amount_debt).strip() and abs(
                _parse_amount(amount_debt, "amount in the debt account's currency") - _parse_amount(amount, "amount")
            ) > 1e-9:
                raise ValueError(f"Both accounts are in {snapshot.base_currency.name}: the amounts must match")
            field, row = _movement_row(
                snapshot, move_date, base_movement, acc_debit, acc_credit, amount, comment, False, "", "", True, False,
            )
            rows, conversion, paid = [(field, row)], None, _amount_of(row["amount"])
        else:
            if _parse_date(move_date, "date") is None:
                raise ValueError("The date is required")
            routed = not other.currency.is_base_currency
            if routed and (not is_foreign(other) or isinstance(other, DebtAccount)):
                raise ValueError(
                    f"A debt movement between two currencies can't be against {other.name}: pick an account in "
                    f"{snapshot.base_currency.name}, or another one in another currency that isn't a debt or "
                    "investment account"
                )
            value = _parse_amount(amount, "amount", NEGATIVE)
            paid = _own_amount(debt, other, value, amount_debt, "is owed")
            if debt.currency.is_base_currency:
                base_value = paid
            elif not routed:
                base_value = value
            else:
                base_value = round(value * _value_on(other.currency, move_date, f"to convert from {other.currency.name}"), 2)
            through = INTERMEDIARY if routed else other.name
            debit, credit = (debt.name, through) if debt.name == acc_debit else (through, debt.name)
            field, row = _movement_row(
                snapshot, move_date, base_movement, debit, credit, base_value, comment, False, "", "", True, False,
            )
            if not debt.currency.is_base_currency:
                row[OTHER_AMOUNT] = f"{paid:,.4f}"
            rows = [(field, row)]
            conversion = (
                _fi_leg(snapshot, move_date, other, other.name == acc_debit, base_value, value, comment) if routed else None
            )
        difference = None
        if remaining_as_difference:
            _, remaining = snapshot.debt_remaining(debt.name if debt else "", base_movement, paid, who.strip())
            difference = Snapshot.difference_of(remaining)
        if difference:
            rows.append(("rows", self._difference_row(snapshot, move_date, *difference, comment, debt)))
        self._add_linked(snapshot, rows, conversion, linked=1)
        return difference

    @staticmethod
    def _difference_row(snapshot, move_date: str, base_name: str, amount: float, comment: str, account: DebtAccount) -> dict:
        """
        The row of a DIFFERENCES movement of this amount in the currency of this debt account:
        out of or into it, against the equity account, at the value of the currency on that
        date, with the amount in its OTHER_AMOUNT if it is in another currency.
        """
        base = snapshot.difference_base_movements().get(base_name)
        if base is None:
            gain = base_name == DIFFERENCE_GAIN
            raise ValueError(
                f"Setting the remaining as a Difference needs {base_name!r}, an internal "
                f"{'gain' if gain else 'loss'} of the base movements YAML"
            )
        debit, credit = (
            (account.name, base.acc_credit.name) if base_name == DIFFERENCE_GAIN else (base.acc_debit.name, account.name)
        )
        rate = _value_on(account.currency, move_date, f"to convert from {account.currency.name}")
        row = _movement_row(
            snapshot, move_date, base_name, debit, credit, round(amount * rate, 2), comment,
            False, "", "", False, False, difference=True,
        )[1]
        return row | ({} if account.currency.is_base_currency else {OTHER_AMOUNT: f"{amount:,.4f}"})

    def delete_debt_movement(self, index: int, fingerprint: str | None = None) -> None:
        """Delete a debt movement of the ledger CSV by position, or a difference added with one."""
        snapshot = self.load()
        rows = snapshot.rows
        difference = 0 <= index < len(rows) and rows.iloc[index]["base_movement"] in snapshot.difference_base_movements()
        self.delete(index, fingerprint, debt=not difference, difference=difference)

    def edit_debt_movement(
        self,
        index: int,
        fingerprint: str | None,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount: str | float,
        who: str,
        comment: str = "",
        amount_debt: str | float = "",
    ) -> None:
        """
        Replace a debt movement of the ledger CSV by position, about `who` like
        add_debt_movement, with its `amount` in the base currency, as it is in the CSV,
        and for a debt account in another currency its `amount_debt`, required there. One
        added with a conversion keeps its date and amount (see edit).
        """
        snapshot = self.load()
        comment = _debt_comment(snapshot, acc_debit, acc_credit, who, comment)
        debt = snapshot.debt_account_of(base_movement, acc_debit, acc_credit)
        amount_other = None
        if debt is not None and is_foreign(debt):
            if not str(amount_debt).strip():
                raise ValueError(f"{debt.name} is in {debt.currency.name}: tell how much is owed in {debt.currency.name}")
            paid = _parse_amount(amount_debt, f"amount in {debt.currency.name}", NEGATIVE)
            if paid == 0:
                raise ValueError(f"The amount in {debt.currency.name} must be positive")
            self._add_column(self.settings.ledger_path, len(snapshot.rows), OTHER_AMOUNT)
            amount_other = f"{paid:,.4f}"
        elif str(amount_debt).strip():
            raise ValueError("Only a debt account in another currency has an amount in its own currency")
        elif OTHER_AMOUNT in snapshot.rows.columns:
            amount_other = ""  # no longer of a debt account in another currency
        self.edit(
            index, fingerprint, move_date, base_movement, acc_debit, acc_credit, amount, comment,
            debt=True, amount_other=amount_other,
        )
