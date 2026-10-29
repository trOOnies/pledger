"""conversion_store.py: the conversions between currencies, cross conversions included."""

from __future__ import annotations

from dataclasses import replace

from pandas import DataFrame, concat

from pledger.web.store.base_store import StoreBase
from pledger.web.store.common import StaleRowError, _with_row, conversion_fingerprint
from pledger.web.store.constants import (
    CONVERSION_COLUMNS,
    CONVERSION_ID,
    CROSS_CONVERSION,
)
from pledger.web.store.conversions import (
    _conversion_row,
    _cross_rows,
    _exchange_args,
    conversion_pairs,
    is_cross,
)
from pledger.web.store.snapshot import Snapshot


class ConversionStore(StoreBase):
    """Adds, edits and deletes the conversions of the conversions CSV."""

    def add_conversion(
        self,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount_base: str | float,
        amount_other: str | float,
        comment: str = "",
    ) -> None:
        """
        Validate a new conversion (see _check_conversion) and append it to the
        conversions CSV, creating it if missing, with the next id of the CSV (see _appended).
        """
        if self.settings.conversions_path is None:
            raise ValueError("No conversions CSV is set")
        snapshot = self.load()
        new_row = _conversion_row(snapshot, move_date, base_movement, acc_debit, acc_credit, amount_base, amount_other, comment)
        self._add_conversions(snapshot, [new_row])

    def _add_conversions(self, snapshot: Snapshot, new_rows: list[dict]) -> None:
        """Append these validated conversions to the conversions CSV, all or none, if the ledger still holds."""
        path = self.settings.conversions_path
        if path is None:
            raise ValueError("No conversions CSV is set")
        conversions = concat([snapshot.conversions, DataFrame(new_rows)], ignore_index=True)
        try:
            replace(snapshot, conversions=conversions).ledger().check_accounts()
        except AssertionError as e:
            raise ValueError(str(e)) from None
        self._write_file(self._appended(path, CONVERSION_ID, new_rows, CONVERSION_COLUMNS))

    def edit_conversion(
        self,
        index: int,
        fingerprint: str | None,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount_base: str | float,
        amount_other: str | float,
        comment: str = "",
    ) -> None:
        """
        Replace a row of the conversions CSV by position with a conversion checked like
        add_conversion, optionally checking it is the expected one. It keeps its id.
        """
        snapshot = self.load()
        self.current_row(snapshot, "conversions", index, fingerprint)
        new_row = _conversion_row(snapshot, move_date, base_movement, acc_debit, acc_credit, amount_base, amount_other, comment)
        new_row = {col: v for col, v in new_row.items() if col in CONVERSION_COLUMNS}  # keep the other columns
        conversions = _with_row(snapshot.conversions, index, new_row)
        try:
            replace(snapshot, conversions=conversions).ledger().check_accounts()
        except AssertionError as e:
            raise ValueError(str(e)) from None
        self._replace_line(snapshot, "conversions", index, new_row)

    def add_exchange(
        self,
        move_date: str,
        sold: str,
        bought: str,
        acc_from: str,
        acc_to: str,
        amount_sold: str | float,
        amount_bought: str | float,
        comment: str = "",
    ) -> str:
        """
        Add a conversion selling one currency for another: from an account in the `sold`
        currency to one in the `bought` currency. With the base one, its base movement is
        picked from the currencies (see exchange_base_movement), and returned. Between two
        other currencies, it is a cross conversion through the INTERMEDIARY account (see
        _cross_rows), and CROSS_CONVERSION is returned.
        """
        snapshot = self.load()
        if is_cross(snapshot, sold, bought):
            self._add_conversions(snapshot, _cross_rows(
                snapshot, move_date, sold, bought, acc_from, acc_to, amount_sold, amount_bought, comment,
            ))
            return CROSS_CONVERSION
        base_movement, *args = _exchange_args(snapshot, sold, bought, acc_from, acc_to, amount_sold, amount_bought)
        self.add_conversion(move_date, base_movement, *args, comment)
        return base_movement

    def edit_exchange(
        self,
        index: int,
        fingerprint: str | None,
        move_date: str,
        sold: str,
        bought: str,
        acc_from: str,
        acc_to: str,
        amount_sold: str | float,
        amount_bought: str | float,
        comment: str = "",
    ) -> str:
        """
        Replace a row of the conversions CSV with a conversion like add_exchange, returning
        its base movement. Both rows of a cross conversion (see conversion_pairs) are
        replaced, with another cross conversion: a conversion doesn't change its kind.
        """
        snapshot = self.load()
        pairs = conversion_pairs(snapshot)
        cross = is_cross(snapshot, sold, bought)
        if cross != (index in pairs):
            kind = "between two other currencies" if index in pairs else f"with {snapshot.base_currency.name}"
            raise ValueError(f"This conversion is {kind} and stays one: delete it and add the new one instead")
        if not cross:
            base_movement, *args = _exchange_args(snapshot, sold, bought, acc_from, acc_to, amount_sold, amount_bought)
            self.edit_conversion(index, fingerprint, move_date, base_movement, *args, comment)
            return base_movement
        self.current_row(snapshot, "conversions", index, fingerprint)
        first = min(index, pairs[index])
        new_rows = _cross_rows(
            snapshot, move_date, sold, bought, acc_from, acc_to, amount_sold, amount_bought, comment,
        )
        new_rows = [{col: v for col, v in row.items() if col in CONVERSION_COLUMNS} for row in new_rows]  # keep the other columns
        conversions = _with_row(_with_row(snapshot.conversions, first, new_rows[0]), first + 1, new_rows[1])
        try:
            replace(snapshot, conversions=conversions).ledger().check_accounts()
        except AssertionError as e:
            raise ValueError(str(e)) from None
        self._replace_line(snapshot, "conversions", first, new_rows[0])
        self._replace_line(snapshot, "conversions", first + 1, new_rows[1])
        return CROSS_CONVERSION

    def delete_conversion(self, index: int, fingerprint: str | None = None) -> None:
        """
        Delete a row of the conversions CSV by position, optionally checking it is the
        expected one: both rows of a cross conversion (see conversion_pairs).
        """
        snapshot = self.load()
        rows = snapshot.conversions
        if not 0 <= index < len(rows):
            raise StaleRowError(f"Conversion {index} does not exist")
        if fingerprint is not None and conversion_fingerprint(rows.iloc[index]) != fingerprint:
            raise StaleRowError("The conversions changed since this page was loaded; reload and try again")
        indexes = sorted({index, conversion_pairs(snapshot).get(index, index)})
        replace(snapshot, conversions=rows.drop(index=rows.index[indexes])).ledger().check_accounts()
        csv_file = self._read_file(len(rows), self.settings.conversions_path)
        for i in reversed(indexes):
            del csv_file.lines[csv_file.row_lines[i]]
        self._write_file(csv_file)
