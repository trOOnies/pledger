"""movement_store.py: the movements of the ledger CSV, and those of the debt account."""

from __future__ import annotations

from dataclasses import replace

from pandas import DataFrame, concat

from pledger.web.store.base_store import StoreBase
from pledger.web.store.common import _fit_row, _with_row
from pledger.web.store.constants import (
    INTEREST_ID,
    INVESTMENT_COLUMN,
    INVESTMENT_ID,
    LEDGER_ID,
)
from pledger.web.store.investments import _check_invest, _check_known_investment
from pledger.web.store.movements import _check_tab, _debt_comment, _movement_row


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
        field, new_row = _movement_row(
            snapshot, move_date, base_movement, acc_debit, acc_credit, amount, comment,
            invest, investment, quantity, debt, card_payment, correction,
        )
        path, what, prefix = {
            "rows": (self.settings.ledger_path, "ledger", LEDGER_ID),
            "interest": (self.settings.interest_path, "interest", INTEREST_ID),
            "investments": (self.settings.investment_movements_path, "investments", INVESTMENT_ID),
        }[field]
        if path is None:
            raise ValueError(f"No {what} CSV is set")
        frame = getattr(snapshot, field)
        new_row = _fit_row(new_row, frame.columns, what)
        changed = replace(snapshot, **{field: concat([frame, DataFrame([new_row])], ignore_index=True)})
        changed.ledger().check_accounts()
        changed.investment_accounts()  # e.g. not selling more than is held
        if field == "investments":
            _check_known_investment(snapshot, acc_debit, acc_credit, new_row[INVESTMENT_COLUMN])
        self._write_file(self._appended(path, prefix, new_row, list(frame.columns)))

    def delete(
        self, index: int, fingerprint: str | None = None, debt: bool = False, card_payment: bool = False,
        correction: bool = False,
    ) -> None:
        """
        Delete a row of the ledger CSV by position, optionally checking it is the
        expected one. Never of an internal base movement, but a debt movement with
        `debt` (from the Debt tab), a card payment with `card_payment` (from the
        Credit Cards tab) or a balance correction with `correction`, which are only
        deleted that way (see _check_tab).
        """
        self._delete_row("rows", index, fingerprint, debt, card_payment, correction)

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
    ) -> None:
        """
        Replace a row of the ledger CSV by position with a movement of the same tab,
        optionally checking it is the expected one, like delete: of the Ledger tab, else
        of the Debt tab with `debt` or of the Credit Cards tab with `card_payment` (see
        _check_tab). The row keeps its id and any other column.
        """
        snapshot = self.load()
        current = self.current_row(snapshot, "rows", index, fingerprint)
        _check_invest(snapshot, current["base_movement"], False, "edited")
        _check_tab(snapshot, current["base_movement"], False, debt, card_payment, "edited")
        _, new_row = _movement_row(
            snapshot, move_date, base_movement, acc_debit, acc_credit, amount, comment,
            False, "", "", debt, card_payment,
        )
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
    ) -> None:
        """
        Add a movement of a debt base movement to the ledger CSV, about `who`, someone of
        its debt account but a company (their debt comes from the work history): its
        comment names them first (prefixed if it doesn't), so the Debt tab adds it to them.
        """
        comment = _debt_comment(self.load(), acc_debit, acc_credit, who, comment)
        self.add(move_date, base_movement, acc_debit, acc_credit, amount, comment, debt=True)

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
