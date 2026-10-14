"""store.py"""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from pandas import DataFrame, read_csv

from pledger.enums import AccountType, MovementType
from pledger.ledger import Ledger
from pledger.models import Account, Movement, MovementBase

BOM = "﻿"


class LedgerDataError(Exception):
    """The ledger files could not be loaded or are inconsistent."""


class StaleRowError(Exception):
    """The row to change no longer matches what the client saw."""


@dataclass(frozen=True)
class Settings:
    """Paths to the ledger files and display options."""

    accounts_path: Path
    base_movements_path: Path
    ledger_path: Path
    currency: str = "$"

    @classmethod
    def from_env(cls) -> Settings:
        """Read paths from PLEDGER_* env vars, defaulting to the info/ folder."""
        return cls(
            accounts_path=Path(os.environ.get("PLEDGER_ACCOUNTS", "info/accounts.yaml")),
            base_movements_path=Path(
                os.environ.get("PLEDGER_BASE_MOVEMENTS", "info/base_movements.yaml")
            ),
            ledger_path=Path(os.environ.get("PLEDGER_LEDGER", "info/ledger.csv")),
            currency=os.environ.get("PLEDGER_CURRENCY", "$"),
        )


@dataclass
class Snapshot:
    """Everything loaded from disk for one request."""

    accounts: dict[str, Account]
    base_movements: dict[str, MovementBase]
    rows: DataFrame  # raw CSV rows, all columns as strings

    def ledger(self, rows: DataFrame | None = None) -> Ledger:
        """Build a Ledger from these rows (all rows by default)."""
        return Ledger.from_dataframe(
            self.rows if rows is None else rows,
            self.accounts,
            self.base_movements,
        )

    def free_accounts(self) -> list[Account]:
        """Accounts that can be picked on a non-fixed side of a movement."""
        return [a for a in self.accounts.values() if a.acc_type != AccountType.EQUITY]


def row_fingerprint(row) -> str:
    """Identifies a row's content, to detect edits made after a page was rendered."""
    return f"{row['move_date']}|{row['base_movement']}|{row['amount']}"


class LedgerStore:
    """Reads and writes the ledger files. The files are the source of truth."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def load(self) -> Snapshot:
        s = self.settings
        try:
            accounts = Account.load_yaml(str(s.accounts_path))
            base_movements = MovementBase.load_yaml(str(s.base_movements_path), accounts)
            rows = read_csv(
                s.ledger_path, sep=";", dtype=str, keep_default_na=False,
                encoding="utf-8-sig",
            )
            snapshot = Snapshot(accounts, base_movements, rows)
            snapshot.ledger()  # validate the whole ledger up front
        except (OSError, KeyError, ValueError, AssertionError) as e:
            raise LedgerDataError(f"{type(e).__name__}: {e}") from e
        return snapshot

    def add(
        self,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount: str | float,
        comment: str = "",
    ) -> None:
        """Validate a new movement and append it to the CSV."""
        snapshot = self.load()
        try:
            dt.date.fromisoformat(move_date)
        except ValueError:
            raise ValueError(f"Invalid date: {move_date!r}") from None
        if base_movement not in snapshot.base_movements:
            raise ValueError(f"Unknown base movement: {base_movement!r}")
        for name in (acc_debit, acc_credit):
            if name not in snapshot.accounts:
                raise ValueError(f"Unknown account: {name!r}")
        try:
            value = float(str(amount).replace(",", "").strip())
        except ValueError:
            raise ValueError(f"Invalid amount: {amount!r}") from None
        if acc_debit == acc_credit:
            raise ValueError("Debit and credit accounts must be different")

        try:
            Movement(
                move_date=move_date,
                move_base=snapshot.base_movements[base_movement],
                input_acc_debit=snapshot.accounts[acc_debit],
                input_acc_credit=snapshot.accounts[acc_credit],
                amount=value,
            )
        except AssertionError as e:
            raise ValueError(str(e)) from None

        new_row = {col: "" for col in snapshot.rows.columns}
        new_row.update(
            move_date=move_date,
            base_movement=base_movement,
            acc_debit=acc_debit,
            acc_credit=acc_credit,
            amount=f"{value:,.2f}",
        )
        if "comment" in new_row:
            new_row["comment"] = comment.replace(";", ",").strip()

        buf = io.StringIO()
        csv.writer(buf, delimiter=";", lineterminator="").writerow(new_row.values())
        csv_file = self._read_file(len(snapshot.rows))
        csv_file.lines.append(buf.getvalue())
        self._write_file(csv_file)

    def add_account(self, name: str, acc_type: str, description: str = "") -> None:
        """Validate a new account and add it to the accounts YAML."""
        snapshot = self.load()
        name = _check_name(name, snapshot.accounts, "account")
        try:
            acc_type = AccountType(acc_type)
        except ValueError:
            raise ValueError(f"Invalid account type: {acc_type!r}") from None
        self._add_yaml_entry(
            self.settings.accounts_path, acc_type.value, name,
            {"description": description.strip()},
        )

    def add_base_movement(
        self,
        name: str,
        move_type: str,
        description: str = "",
        debit: str = "",
        credit: str = "",
    ) -> None:
        """Validate a new base movement and add it to the base movements YAML."""
        snapshot = self.load()
        name = _check_name(name, snapshot.base_movements, "base movement")
        try:
            move_type = MovementType(move_type)
        except ValueError:
            raise ValueError(f"Invalid movement type: {move_type!r}") from None
        # Gains always credit equity and losses always debit it.
        if move_type == MovementType.GAIN:
            credit = ""
        elif move_type == MovementType.LOSS:
            debit = ""
        free = {a.name for a in snapshot.free_accounts()}
        for acc in (debit, credit):
            if acc and acc not in free:
                raise ValueError(f"Unknown or non-selectable account: {acc!r}")
        if debit and debit == credit:
            raise ValueError("Debit and credit accounts must be different")

        fields = {"description": description.strip()}
        if debit:
            fields["debit"] = debit
        if credit:
            fields["credit"] = credit
        self._add_yaml_entry(
            self.settings.base_movements_path, move_type.value, name, fields
        )

    def _add_yaml_entry(self, path: Path, section: str, name: str, fields: dict) -> None:
        """
        Insert `name: fields` at the end of a top-level section, as text, so the
        rest of the file (comments, extra keys, ordering) is left as is. Rolls
        back if the files no longer load.
        """
        before = path.read_bytes().decode("utf-8")
        _atomic_write(path, _insert_yaml_entry(before, section, name, fields))
        try:
            self.load()
        except LedgerDataError as e:
            _atomic_write(path, before)
            raise ValueError(f"Could not add {name!r}: {e}") from None

    def delete(self, index: int, fingerprint: str | None = None) -> None:
        """Delete a row by position, optionally checking it is the expected one."""
        rows = self.load().rows
        if not 0 <= index < len(rows):
            raise StaleRowError(f"Row {index} does not exist")
        if fingerprint is not None and row_fingerprint(rows.iloc[index]) != fingerprint:
            raise StaleRowError(
                "The ledger changed since this page was loaded; reload and try again"
            )
        csv_file = self._read_file(len(rows))
        del csv_file.lines[csv_file.row_lines[index]]
        self._write_file(csv_file)

    def _read_file(self, n_rows: int) -> _CsvFile:
        """
        Split the CSV into raw lines, so edits touch only the affected line and
        every other byte (quoting, BOM, line endings) is kept as is.
        """
        raw = self.settings.ledger_path.read_bytes().decode("utf-8")
        bom = raw.startswith(BOM)
        text = raw.removeprefix(BOM)
        newline = "\r\n" if "\r\n" in text else "\n"
        trailing = text.endswith(newline)
        lines = text.removesuffix(newline).split(newline) if text else []
        # Line 0 is the header; blank lines are skipped by the CSV reader.
        row_lines = [i for i, line in enumerate(lines) if i > 0 and line.strip()]
        if len(row_lines) != n_rows:
            raise LedgerDataError(
                "The ledger CSV has multi-line fields; edit it outside pledger"
            )
        return _CsvFile(lines, row_lines, newline, bom, trailing)

    def _write_file(self, csv_file: _CsvFile) -> None:
        """Atomically replace the CSV."""
        text = csv_file.newline.join(csv_file.lines)
        if csv_file.trailing:
            text += csv_file.newline
        if csv_file.bom:
            text = BOM + text
        _atomic_write(self.settings.ledger_path, text)


def _atomic_write(path: Path, text: str) -> None:
    """Atomically replace a file, keeping its permissions."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _check_name(name: str, existing: dict, what: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError(f"The {what} name is required")
    if any(c in name for c in ";\r\n"):
        raise ValueError(f"The {what} name cannot contain ';' or line breaks")
    if name in existing:
        raise ValueError(f"{what.capitalize()} {name!r} already exists")
    return name


def _insert_yaml_entry(text: str, section: str, name: str, fields: dict) -> str:
    """Return `text` with `name: fields` appended to the top-level `section`."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    header = re.compile(rf"^{re.escape(section)}:\s*(#.*)?$")
    start = next((i for i, line in enumerate(lines) if header.match(line)), None)

    if start is None:
        if any(line.startswith(f"{section}:") for line in lines):
            raise ValueError(f"Cannot add to the {section!r} section; edit the file by hand")
        while lines and not lines[-1].strip():
            lines.pop()
        return newline.join([*lines, f"{section}:", *_yaml_entry(name, fields, 2), ""])

    # The section ends at the next top-level key; insert after its last indented line.
    last, indent = start, None
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and line[0] not in " \t#":
            break
        if line.strip() and line[0] in " \t" and not line.lstrip().startswith("#"):
            last = i
            if indent is None:
                indent = len(line) - len(line.lstrip())
    lines[last + 1:last + 1] = _yaml_entry(name, fields, indent or 2)
    return newline.join(lines)


def _yaml_entry(name: str, fields: dict, indent: int) -> list[str]:
    dumped = yaml.safe_dump(
        {name: fields}, sort_keys=False, allow_unicode=True,
        default_flow_style=False, indent=indent, width=10**9,
    )
    return [" " * indent + line for line in dumped.splitlines()]


@dataclass
class _CsvFile:
    lines: list[str]
    row_lines: list[int]  # line number of each data row
    newline: str
    bom: bool
    trailing: bool
