"""movement_store.py: the movements of the ledger CSV, and those of the debt account."""

from __future__ import annotations

from dataclasses import replace

from pandas import DataFrame, concat

from pledger.models import DEBT_ACCOUNT, CreditCardAccount, is_foreign
from pledger.web.store.base_store import StoreBase
from pledger.web.store.common import _amount_of, _fit_row, _parse_amount, _with_row
from pledger.web.store.constants import DIFFERENCE_GAIN, INVESTMENT_COLUMN, OTHER_AMOUNT
from pledger.web.store.investments import _check_invest, _check_known_investment
from pledger.web.store.movements import _check_tab, _debt_comment, _movement_row
from pledger.web.store.snapshot import Snapshot


def foreign_card(snapshot, acc_debit: str, acc_credit: str) -> CreditCardAccount | None:
    """The credit card in another currency on a side, if any."""
    accounts = (snapshot.accounts.get(n) for n in (acc_debit, acc_credit))
    return next((a for a in accounts if isinstance(a, CreditCardAccount) and is_foreign(a)), None)


class MovementStore(StoreBase):
    """Adds, edits and deletes the movements of the ledger CSV."""

    def add(
        self,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount: str | float,
        comment: str = "",
        invest: bool = False,
        investment: str = "",
        quantity: str | float = "",
        debt: bool = False,
        card_payment: bool = False,
        correction: bool = False,
    ) -> None:
        """
        Validate a new movement and append it to its CSV. With `invest` (from the
        Investments tab), it must be of an investment base movement (see
        Snapshot.invest_base_movements): the interest on money goes to the interest CSV,
        and the others to the investments CSV, with their investment and quantity (see
        _investment_details); an investment of an investment account must be one it has
        (see _check_known_investment). Without it, it must be of a non-internal base
        movement whose movements aren't generated, and goes to the ledger CSV: one of a
        debt base movement with `debt` (from the Debt tab), one of a card payment base
        movement with `card_payment` (from the Credit Cards tab), a balance correction with
        `correction` (from the Audit tab), else one of the Ledger tab (see _check_tab). It gets the next id of its CSV (see _appended).
        """
        snapshot = self.load()
        if card_payment and foreign_card(snapshot, acc_debit, acc_credit):
            raise ValueError("A card in another currency needs what was paid in its currency (see add_card_payment)")
        field, new_row = _movement_row(
            snapshot, move_date, base_movement, acc_debit, acc_credit, amount, comment,
            invest, investment, quantity, debt, card_payment, correction,
        )
        if field == "investments":
            _check_known_investment(snapshot, acc_debit, acc_credit, new_row[INVESTMENT_COLUMN])
        self._append_rows(snapshot, field, [new_row])

    def _append_rows(self, snapshot, field: str, new_rows: list[dict]) -> None:
        """Check these rows of a CSV of movements, then append them to it together, or none of them."""
        path, what, prefix = self._movement_file(field)
        frame = getattr(snapshot, field)
        new_rows = [_fit_row(row, frame.columns, what) for row in new_rows]
        changed = replace(snapshot, **{field: concat([frame, DataFrame(new_rows)], ignore_index=True)})
        changed.ledger().check_accounts()
        changed.investment_accounts()  # e.g. not selling more than is held
        self._write_file(self._appended(path, prefix, new_rows, list(frame.columns)))

    def delete(
        self, index: int, fingerprint: str | None = None, debt: bool = False, card_payment: bool = False,
        correction: bool = False, difference: bool = False,
    ) -> None:
        """
        Delete a row of the ledger CSV by position, optionally checking it is the
        expected one. Never of an internal base movement, but a debt movement with
        `debt` (from the Debt tab), a card payment with `card_payment` (from the
        Credit Cards tab), a balance correction with `correction` or a Difference with
        `difference`, which are only deleted that way (see _check_tab).
        """
        self._delete_row("rows", index, fingerprint, debt, card_payment, correction, difference)

    def edit(
        self,
        index: int,
        fingerprint: str | None,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount: str | float,
        comment: str = "",
        debt: bool = False,
        card_payment: bool = False,
        amount_card: str = "",
    ) -> None:
        """
        Replace a row of the ledger CSV by position with a movement of the same tab,
        optionally checking it is the expected one, like delete: of the Ledger tab, else
        of the Debt tab with `debt` or of the Credit Cards tab with `card_payment` (see
        _check_tab). The row keeps its id and any other column. A card payment of a card in
        another currency has its `amount_card` in that currency (see LinkedStore.edit_card_payment).
        """
        snapshot = self.load()
        current = self.current_row(snapshot, "rows", index, fingerprint)
        if self._linked_group(snapshot, "rows", index) and not card_payment and (
            move_date != current["move_date"] or _parse_amount(amount, "amount") != _amount_of(current["amount"])
        ):
            raise ValueError("Its date and amount came with its conversion: delete the movement and add it again")
        _check_invest(snapshot, current["base_movement"], False, "edited")
        _check_tab(snapshot, current["base_movement"], False, debt, card_payment, "edited")
        _, new_row = _movement_row(
            snapshot, move_date, base_movement, acc_debit, acc_credit, amount, comment,
            False, "", "", debt, card_payment,
        )
        if amount_card:
            new_row[OTHER_AMOUNT] = amount_card
        _fit_row(new_row, snapshot.rows.columns, "ledger")  # every value has a column
        replace(snapshot, rows=_with_row(snapshot.rows, index, new_row)).ledger().check_accounts()
        self._replace_line(snapshot, "rows", index, new_row)

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
    ) -> tuple[str, float] | None:
        """
        Add a movement of a debt base movement to the ledger CSV, about `who`, someone of
        its debt account but a company (their debt comes from the work history): its
        comment names them first (prefixed if it doesn't), so the Debt tab adds it to them.

        With `remaining_as_difference`, what would remain with them after it, in all the
        ledger, is settled by a DIFFERENCES movement of the same date and comment, against
        the equity account, added together with it (see Snapshot.difference_of). Returns
        its base movement and amount, None if nothing remains or it wasn't asked for.
        """
        snapshot = self.load()
        comment = _debt_comment(snapshot, acc_debit, acc_credit, who, comment)
        field, new_row = _movement_row(
            snapshot, move_date, base_movement, acc_debit, acc_credit, amount, comment,
            False, "", "", True, False,
        )
        new_rows = [new_row]
        difference = None
        if remaining_as_difference:
            _, remaining = snapshot.debt_remaining(base_movement, _amount_of(new_row["amount"]), who.strip())
            difference = Snapshot.difference_of(remaining)
        if difference:
            new_rows.append(self._difference_row(snapshot, move_date, *difference, comment))
        self._append_rows(snapshot, field, new_rows)
        return difference

    @staticmethod
    def _difference_row(snapshot, move_date: str, base_name: str, amount: float, comment: str) -> dict:
        """The row of a DIFFERENCES movement: out of or into the debt account, against the equity account."""
        base = snapshot.difference_base_movements().get(base_name)
        if base is None:
            gain = base_name == DIFFERENCE_GAIN
            raise ValueError(
                f"Setting the remaining as a Difference needs {base_name!r}, an internal "
                f"{'gain' if gain else 'loss'} of the base movements YAML"
            )
        debit, credit = (
            (DEBT_ACCOUNT, base.acc_credit.name) if base_name == DIFFERENCE_GAIN else (base.acc_debit.name, DEBT_ACCOUNT)
        )
        return _movement_row(
            snapshot, move_date, base_name, debit, credit, amount, comment,
            False, "", "", False, False, difference=True,
        )[1]

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
    ) -> None:
        """Replace a debt movement of the ledger CSV by position, about `who` like add_debt_movement."""
        comment = _debt_comment(self.load(), acc_debit, acc_credit, who, comment)
        self.edit(index, fingerprint, move_date, base_movement, acc_debit, acc_credit, amount, comment, debt=True)
