"""base_store.py: loading the data files, and reading and writing the rows of their CSVs."""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from pandas import DataFrame, Series, read_csv

from pledger.models import (
    Account,
    Currency,
    MovementBase,
    WorkMonth,
    load_previous_month,
)
from pledger.web.store import common
from pledger.web.store.common import (
    LedgerDataError,
    StaleRowError,
    _atomic_write,
    _CsvFile,
    _existing,
    _load_movements,
    conversion_fingerprint,
    interest_fingerprint,
    next_id,
    row_fingerprint,
    stored_ids,
)
from pledger.web.store.constants import (
    BOM,
    CONVERSION_COLUMNS,
    CONVERSION_ID,
    GENERATED,
    ID_COLUMN,
    INTEREST_COLUMNS,
    INTEREST_ID,
    INVESTMENT_COLUMNS,
    INVESTMENT_ID,
    LEDGER_ID,
    PAYMENT_DATE,
    PRICE_COLUMNS,
    SERVICE_MONTH,
    WORK_COLUMNS,
)
from pledger.web.store.investments import _check_invest, initial_prices
from pledger.web.store.movements import _check_initial_base_movements, _check_tab
from pledger.web.store.snapshot import Snapshot
from pledger.web.store.work import Work, _work_month, deduction_columns

if TYPE_CHECKING:
    from pledger.web.store.settings import Settings


class StoreBase:
    """What every part of the store shares: the settings, the snapshot of the data files and their rows."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._cached: tuple[tuple, Snapshot] | None = None  # see load

    def _files_state(self) -> tuple:
        """What tells whether the data files changed: their mtime, size and inode, and today."""
        state = []
        for path in self.settings.expected_files():
            try:
                stat = path.stat()
                state.append((str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino))
            except OSError:
                state.append((str(path), None))
        return (common.today(), *state)

    def load(self) -> Snapshot:
        """
        The snapshot of the data files, reused while none of them change (see _files_state):
        nothing changes a snapshot once loaded, so the requests of a page can share it.
        """
        state = self._files_state()
        if self._cached is not None and self._cached[0] == state:
            return self._cached[1]
        snapshot = self._load()
        self._cached = (state, snapshot)
        return snapshot

    def _load(self) -> Snapshot:
        s = self.settings
        try:
            base_currency, other_currencies = Currency.load_yaml(str(s.currencies_path))
            currencies = {base_currency.name: base_currency, **other_currencies}
            work = self.load_work()
            prices = _load_movements(s.prices_path, PRICE_COLUMNS, "prices")
            services = _load_movements(s.services_path, [SERVICE_MONTH], "services")
            accounts = Account.load_yaml(
                str(s.accounts_path), base_currency, other_currencies,
                _existing(s.investments_path), _existing(s.creditors_path),
                list(work.companies), _existing(s.credit_cards_path), initial_prices(prices),
            )
            base_movements, result_types = MovementBase.load_yaml(
                str(s.base_movements_path), accounts
            )
            _check_initial_base_movements(base_movements)
            rows = read_csv(
                s.ledger_path, sep=";", dtype=str, keep_default_na=False,
                encoding="utf-8-sig",
            )
            stored = [n for n in GENERATED if n in set(rows["base_movement"])]
            if stored:
                raise ValueError(
                    f"The ledger CSV cannot have movements of {stored}: they are generated from the "
                    "initial amounts of the accounts, the values of the currencies and the work history"
                )
            snapshot = Snapshot(
                accounts, base_movements, result_types, rows, currencies, self.load_conversions(),
                _load_movements(s.investment_movements_path, INVESTMENT_COLUMNS, "investments"),
                _load_movements(s.interest_path, INTEREST_COLUMNS, "interest"),
                prices, services, work, load_previous_month(str(s.accounts_path)),
            )
            misplaced = sorted(set(rows["base_movement"]) & set(snapshot.invest_base_movements()))
            if misplaced:
                raise ValueError(
                    f"The ledger CSV cannot have movements of {misplaced}: they go in the investments "
                    "CSV, or in the interest CSV for the interest on money"
                )
            snapshot.ledger().check_accounts()  # validate the whole ledger up front
            snapshot.investment_accounts()
        except (OSError, KeyError, ValueError, AssertionError) as e:
            raise LedgerDataError(f"{type(e).__name__}: {e}") from e
        return snapshot

    def freeze_ids(self) -> None:
        """
        Give a new id to each row of the CSVs of movements without a valid one (see
        stored_ids), adding their ID_COLUMN if missing. Run on startup and when
        adding a movement to a CSV, so the rows added to the CSVs by hand get theirs too.
        """
        s = self.settings
        for path, prefix in (
            (s.ledger_path, LEDGER_ID),
            (s.conversions_path, CONVERSION_ID),
            (s.investment_movements_path, INVESTMENT_ID),
            (s.interest_path, INTEREST_ID),
        ):
            if _existing(path) and path.read_bytes().strip():
                self._freeze_file(path, prefix)

    def _freeze_file(self, path: Path, prefix: str) -> None:
        rows = read_csv(path, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
        ids = stored_ids(rows, prefix)
        if ID_COLUMN in rows.columns and all(ids):
            return
        csv_file = self._read_file(len(rows), path)
        lines = csv_file.lines
        header = next(csv.reader([lines[0]], delimiter=";"))
        if ID_COLUMN not in header:
            header.append(ID_COLUMN)
            lines[0] += f";{ID_COLUMN}"
        col = header.index(ID_COLUMN)
        number = int(next_id(rows, prefix).split("-")[1])
        for i, current in zip(csv_file.row_lines, ids):
            if current:
                continue
            new, number = f"{prefix}-{number}", number + 1
            fields = next(csv.reader([lines[i]], delimiter=";"))
            if len(fields) <= col == len(header) - 1:
                # The id is the last column and the line stops before it: keep its bytes.
                lines[i] += ";" * (col - len(fields) + 1) + new
            else:
                fields += [""] * (len(header) - len(fields))
                fields[col] = new
                buf = io.StringIO()
                csv.writer(buf, delimiter=";", lineterminator="").writerow(fields)
                lines[i] = buf.getvalue()
        self._write_file(csv_file)

    def load_cpi(self) -> Series | None:
        """The daily CPI of the base currency, or None if the currencies can't be read."""
        try:
            base_currency, _ = Currency.load_yaml(str(self.settings.currencies_path))
        except (OSError, KeyError, ValueError, AssertionError):
            return None
        return base_currency.customer_price_index

    def load_work(self) -> Work:
        """
        The work history: one row per month, in order and with no missing months
        (a month without a job is a row with no company, position or salary).
        """
        path = self.settings.work_path
        try:
            if _existing(path) and path.read_bytes().strip():
                rows = read_csv(path, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
                missing = [c for c in WORK_COLUMNS if c != PAYMENT_DATE and c not in rows.columns]
                if missing:
                    raise ValueError(f"The work CSV is missing the columns {missing}")
            else:
                rows = DataFrame(columns=WORK_COLUMNS, dtype=str)
            deductions = deduction_columns(rows.columns)
            months = [_work_month(row, deductions) for _, row in rows.iterrows()]
            WorkMonth.check_sequence(months)
        except (OSError, KeyError, ValueError) as e:
            raise LedgerDataError(f"{type(e).__name__}: {e}") from e
        return Work(months, rows)

    def current_row(self, snapshot: Snapshot, field: str, index: int, fingerprint: str | None):
        """The row of the CSV of a Snapshot `field` at this position, if it is the expected one."""
        rows = getattr(snapshot, field)
        what, _, fingerprint_of = self._csv_of(field)
        if not 0 <= index < len(rows):
            raise StaleRowError(f"Row {index} of the {what} does not exist")
        if fingerprint is not None and fingerprint_of(rows.iloc[index]) != fingerprint:
            raise StaleRowError(f"The {what} changed since this page was loaded; reload and try again")
        return rows.iloc[index]

    def _csv_of(self, field: str) -> tuple[str, Path, Callable]:
        """What the CSV of a Snapshot `field` holds, its path and how its rows are fingerprinted."""
        s = self.settings
        return {
            "rows": ("ledger", s.ledger_path, row_fingerprint),
            "investments": ("investments", s.investment_movements_path, row_fingerprint),
            "interest": ("interest on money", s.interest_path, interest_fingerprint),
            "conversions": ("conversions", s.conversions_path, conversion_fingerprint),
        }[field]

    def _replace_line(self, snapshot: Snapshot, field: str, index: int, new_row: dict) -> None:
        """
        Replace the values of `new_row` in the line of a row of the CSV of a Snapshot
        `field`, keeping its id and other columns.
        """
        csv_file = self._read_file(len(getattr(snapshot, field)), self._csv_of(field)[1])
        line = csv_file.row_lines[index]
        header = next(csv.reader([csv_file.lines[0]], delimiter=";"))
        fields = next(csv.reader([csv_file.lines[line]], delimiter=";"))
        fields += [""] * (len(header) - len(fields))
        for col, value in new_row.items():
            if col in header and col != ID_COLUMN:
                fields[header.index(col)] = value
        buf = io.StringIO()
        csv.writer(buf, delimiter=";", lineterminator="").writerow(fields)
        csv_file.lines[line] = buf.getvalue()
        self._write_file(csv_file)

    def _delete_row(
        self, field: str, index: int, fingerprint: str | None, debt: bool = False, card_payment: bool = False,
        correction: bool = False,
    ) -> None:
        """
        Delete a row by position from the CSV of a Snapshot `field` ("rows",
        "investments", "interest" or "services"), optionally checking it is the expected one.
        """
        snapshot = self.load()
        rows = getattr(snapshot, field)
        path = self._csv_of(field)[1]
        self.current_row(snapshot, field, index, fingerprint)
        if field == "rows":
            _check_invest(snapshot, rows.iloc[index]["base_movement"], False, "deleted")
            _check_tab(snapshot, rows.iloc[index]["base_movement"], False, debt, card_payment, "deleted", correction)
        changed = replace(snapshot, **{field: rows.drop(index=rows.index[index])})
        changed.ledger().check_accounts()
        changed.investment_accounts()  # e.g. not deleting a purchase of something sold later
        csv_file = self._read_file(len(rows), path)
        del csv_file.lines[csv_file.row_lines[index]]
        self._write_file(csv_file)

    def load_conversions(self) -> DataFrame:
        """The conversions CSV rows, all columns as strings; none if it is missing."""
        return _load_movements(self.settings.conversions_path, CONVERSION_COLUMNS, "conversions")

    def _appended(self, path: Path, prefix: str, row: dict | list[dict], header: list[str]) -> _CsvFile:
        """
        A CSV of movements with its ids frozen (see freeze_ids) and this row (or these
        rows, in order) appended with the next ids, ready to write. A missing or empty
        CSV is a new one with this header and the ID_COLUMN.
        """
        if _existing(path) and path.read_bytes().strip():
            self._freeze_file(path, prefix)
            rows = read_csv(path, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
            columns = list(rows.columns)
        else:
            columns = [c for c in header if c != ID_COLUMN] + [ID_COLUMN]
            rows = DataFrame(columns=columns, dtype=str)
        number = int(next_id(rows, prefix).split("-")[1])
        csv_file = self._read_file(len(rows), path, header=columns)
        for i, new_row in enumerate([row] if isinstance(row, dict) else row):
            values = new_row | {ID_COLUMN: f"{prefix}-{number + i}"}
            buf = io.StringIO()
            csv.writer(buf, delimiter=";", lineterminator="").writerow([values.get(c, "") for c in columns])
            csv_file.lines.append(buf.getvalue())
        return csv_file

    def _read_file(self, n_rows: int, path: Path, header: list[str] | None = None) -> _CsvFile:
        """
        Split a CSV into raw lines, so edits touch only the affected line and
        every other byte (quoting, BOM, line endings) is kept as is. With a `header`,
        a missing or empty CSV is a new one with it.
        """
        raw = path.read_bytes().decode("utf-8") if path.exists() else ""
        if header is not None and not raw.strip():
            raw = ";".join(header) + "\n"
        bom = raw.startswith(BOM)
        text = raw.removeprefix(BOM)
        newline = "\r\n" if "\r\n" in text else "\n"
        trailing = text.endswith(newline)
        lines = text.removesuffix(newline).split(newline) if text else []
        # Line 0 is the header; blank lines are skipped by the CSV reader.
        row_lines = [i for i, line in enumerate(lines) if i > 0 and line.strip()]
        if len(row_lines) != n_rows:
            raise LedgerDataError(
                f"The CSV {path} has multi-line fields; edit it outside pledger"
            )
        return _CsvFile(path, lines, row_lines, newline, bom, trailing)

    def _write_file(self, csv_file: _CsvFile) -> None:
        """Atomically replace a CSV."""
        text = csv_file.newline.join(csv_file.lines)
        if csv_file.trailing:
            text += csv_file.newline
        if csv_file.bom:
            text = BOM + text
        _atomic_write(csv_file.path, text)
