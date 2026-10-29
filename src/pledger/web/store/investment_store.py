"""investment_store.py: the movements of the investments, their prices and the interest on money."""

from __future__ import annotations

import csv
import io
from dataclasses import replace

from pandas import DataFrame, concat

from pledger.enums import InvestmentType
from pledger.models import InvestmentAccount
from pledger.web.store.base_store import StoreBase
from pledger.web.store.common import (
    LedgerDataError,
    _atomic_write,
    _check_name,
    _fit_row,
    _parse_amount,
    _parse_date,
    _quantity_text,
    _with_row,
)
from pledger.web.store.constants import INITIAL_PRICE, INVESTMENT_COLUMN, PRICE_COLUMNS
from pledger.web.store.investments import _check_known_investment
from pledger.web.store.movements import _movement_row
from pledger.web.store.snapshot import Snapshot
from pledger.web.store.yaml_edit import _insert_yaml_entry


class InvestmentStore(StoreBase):
    """Adds, edits and deletes the movements of the investments and the interest, and adds prices."""

    def add_investment(
        self, account: str, name: str, investment_type: str, quantity: str | float = "", price: str | float = ""
    ) -> None:
        """
        Add an investment to an investment account in the investments YAML, with its
        type and, optionally, its initial quantity (0 by default), which needs its
        initial price, added to the prices CSV (see Account.load_yaml). An investment
        only in the ledger gets its type this way; one in the YAML already has it.
        """
        snapshot = self.load()
        path, prices_path = self.settings.investments_path, self.settings.prices_path
        if path is None:
            raise ValueError("No investments YAML is set")
        accounts = {a.name: a for a in snapshot.accounts.values() if isinstance(a, InvestmentAccount)}
        if account not in accounts:
            raise ValueError(f"{account!r} is not an investment account")
        name = _check_name(name, accounts[account].investments, "investment")
        try:
            inv_type = InvestmentType(investment_type)
        except ValueError:
            raise ValueError(f"Unknown investment type: {investment_type!r}") from None
        units = _parse_amount(quantity, "initial quantity") if str(quantity).strip() else 0.0
        value = _parse_amount(price, "initial price") if str(price).strip() else None
        if units and value is None:
            raise ValueError("An initial quantity needs its initial price")
        if value is not None and prices_path is None:
            raise ValueError("No prices CSV is set")
        fields: str | dict = {"type": inv_type.value, "quantity": units} if units else inv_type.value
        before = path.read_bytes().decode("utf-8") if path.exists() else None
        prices_before = prices_path.read_bytes() if prices_path is not None and prices_path.exists() else None
        _atomic_write(path, _insert_yaml_entry(before or "", (account,), name, fields))
        try:
            if value is not None:
                self._append_price(snapshot, INITIAL_PRICE, account, name, value)
            self.load()
        except (LedgerDataError, ValueError) as e:
            if before is None:
                path.unlink()
            else:
                _atomic_write(path, before)
            if value is not None and prices_before is None:
                prices_path.unlink(missing_ok=True)
            elif value is not None:
                _atomic_write(prices_path, prices_before.decode("utf-8"))
            raise ValueError(f"Could not add {name!r}: {e}") from None

    def add_price(self, date: str, account: str, investment: str, price: str | float) -> None:
        """
        Add a price of an investment of an investment account on a date to the prices
        CSV: it is revalued at it at the end of that date (see _capital_rows). An
        investment has one price on each date at most.
        """
        snapshot = self.load()
        if _parse_date(date, "date") is None:
            raise ValueError("The date is required")
        accounts = {a.name: a for a in snapshot.investment_accounts()}
        if account not in accounts:
            raise ValueError(f"{account!r} is not an investment account")
        investment = investment.strip()
        if investment not in accounts[account].investments:
            raise ValueError(f"{investment!r} is not an investment of {account}: add it as a new investment first")
        value = _parse_amount(price, "price")
        date = date.strip()
        taken = zip(snapshot.prices["date"], snapshot.prices["account"], snapshot.prices[INVESTMENT_COLUMN])
        if (date, account, investment) in {(d.strip(), a.strip(), n.strip()) for d, a, n in taken}:
            raise ValueError(f"{investment} of {account} already has a price on {date}: change it in the prices CSV")
        new_row = {"date": date, "account": account, INVESTMENT_COLUMN: investment, "price": _quantity_text(value)}
        changed = replace(snapshot, prices=concat([snapshot.prices, DataFrame([new_row])], ignore_index=True))
        changed.ledger().check_accounts()
        changed.investment_accounts()
        self._append_price(snapshot, date, account, investment, value)

    def _append_price(self, snapshot: Snapshot, date: str, account: str, investment: str, price: float) -> None:
        """Append a price to the prices CSV, created with its header if missing."""
        path = self.settings.prices_path
        if path is None:
            raise ValueError("No prices CSV is set")
        csv_file = self._read_file(len(snapshot.prices), path, header=PRICE_COLUMNS)
        header = next(csv.reader([csv_file.lines[0]], delimiter=";"))
        values = {"date": date, "account": account, INVESTMENT_COLUMN: investment, "price": _quantity_text(price)}
        buf = io.StringIO()
        csv.writer(buf, delimiter=";", lineterminator="").writerow([values.get(c, "") for c in header])
        csv_file.lines.append(buf.getvalue())
        self._write_file(csv_file)

    def edit_investment(
        self,
        source: str,
        index: int,
        fingerprint: str | None,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount: str | float,
        comment: str = "",
        investment: str = "",
        quantity: str | float = "",
    ) -> None:
        """
        Replace a row of the investments CSV, or of the interest CSV with `source`
        "interest", by position with a movement of the Investments tab, checked like
        add. It stays in its CSV, and keeps its id and any other column.
        """
        snapshot = self.load()
        self.current_row(snapshot, source, index, fingerprint)
        field, new_row = _movement_row(
            snapshot, move_date, base_movement, acc_debit, acc_credit, amount, comment,
            True, investment, quantity, False, False,
        )
        if field != source:
            where = {"interest": "interest on money", "investments": "investments"}
            raise ValueError(
                f"A movement of the {where[source]} can't become one of the {where[field]}: "
                "delete it and add it again instead"
            )
        frame = getattr(snapshot, field)
        _fit_row(new_row, frame.columns, field)  # every value has a column
        changed = replace(snapshot, **{field: _with_row(frame, index, new_row)})
        changed.ledger().check_accounts()
        changed.investment_accounts()  # e.g. not selling more than is held
        if field == "investments":
            _check_known_investment(snapshot, acc_debit, acc_credit, new_row[INVESTMENT_COLUMN])
        self._replace_line(snapshot, field, index, new_row)

    def delete_investment(self, index: int, fingerprint: str | None = None) -> None:
        """Delete a row of the investments CSV by position, like delete."""
        self._delete_row("investments", index, fingerprint)

    def delete_interest(self, index: int, fingerprint: str | None = None) -> None:
        """Delete a row of the interest CSV by position, like delete."""
        self._delete_row("interest", index, fingerprint)
