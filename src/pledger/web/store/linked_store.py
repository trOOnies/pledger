"""linked_store.py: the movements of several rows: broker fees, and paying or being paid in other currencies."""

from __future__ import annotations

from dataclasses import replace

from pandas import DataFrame, concat, read_csv

from pledger.enums import AccountType, LedgerSide, MovementType
from pledger.models import (
    INTERMEDIARY,
    Account,
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
from pledger.web.store.movements import _movement_row
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


class LinkedStore(MovementStore):
    """Adds the movements that take several rows, through the INTERMEDIARY account if they change currency."""

    def add_ledger_movement(
        self, move_date: str, base_movement: str, acc_debit: str, acc_credit: str, amount: str | float, comment: str = "",
    ) -> None:
        """
        Add a movement of the Ledger tab (see add). The `amount` of a gain or loss of an
        account in another currency is in that currency: at its value on that date, the
        movement goes through the INTERMEDIARY account, in the base currency, with a
        conversion linked to it. A base movement with that account fixed can't: it is
        a single row, of the amount in the base currency. Transfers are in the base one.
        """
        snapshot = self.load()
        account = foreign_result_account(snapshot, base_movement, acc_debit, acc_credit)
        if account is None:
            self.add(move_date, base_movement, acc_debit, acc_credit, amount, comment)
            return
        if _parse_date(move_date, "date") is None:
            raise ValueError("The date is required")
        value = _parse_amount(amount, "amount", NEGATIVE)
        base = snapshot.base_movements[base_movement]
        rate = _value_on(account.currency, move_date, f"to convert from {account.currency.name}")
        base_value = round(value * rate, 2)
        inflow = account.name == acc_debit
        if base.side(LedgerSide.DEBIT if inflow else LedgerSide.CREDIT) is not None:
            self.add(move_date, base_movement, acc_debit, acc_credit, f"{base_value:.2f}", comment)
            return
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
            and base_movement not in INTEREST
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

    def _add_linked(self, snapshot: Snapshot, rows: list[tuple[str, dict]], conversion: dict | None) -> None:
        """
        Append these rows ((Snapshot field, row)) to their CSVs, and this conversion with
        their ids in its LINKED_COLUMN, once they hold together.
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
            row = conversion | {LINKED_COLUMN: " ".join(ids)}
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
        value = _parse_amount(amount, "amount", NEGATIVE)
        paid = self._card_amount(card, other, value, amount_card)
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

    @staticmethod
    def _card_amount(card: Account, other: Account, value: float, amount_card: str | float) -> float:
        """What was paid in the currency of a card, which the other account's may already say."""
        same = other.currency.name == card.currency.name
        if not str(amount_card).strip():
            if not same:
                raise ValueError(
                    f"{card.name} is in {card.currency.name}: tell how much debt was paid in {card.currency.name}"
                )
            return value
        paid = _parse_amount(amount_card, f"amount in {card.currency.name}")
        if same and abs(paid - value) > 1e-9:
            raise ValueError(f"{other.name} and {card.name} are both in {card.currency.name}: the amounts must match")
        if paid == 0:
            raise ValueError(f"The amount in {card.currency.name} must be positive")
        return paid

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
            card_payment=True, amount_card=f"{paid:,.4f}",
        )
