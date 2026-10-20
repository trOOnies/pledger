"""store.py"""

from __future__ import annotations

import csv
import datetime as dt
import io
import math
import os
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

import yaml
from pandas import DataFrame, concat, read_csv

from pledger.enums import AccountType, LedgerSide, MovementType
from pledger.ledger import Ledger
from pledger.models import (
    SPECIAL_SIDES,
    Account,
    Company,
    Currency,
    DebtAccount,
    JobPosition,
    Movement,
    MovementBase,
    ResultType,
    WorkMonth,
    next_month,
    parse_month,
)

BOM = "﻿"

# Internal base movements recording the initial amount of the accounts. Their
# movements are generated from the accounts YAML, never stored in the CSV.
INITIAL_GAIN = "Initial (G)"
INITIAL_LOSS = "Initial (L)"


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
    currencies_path: Path
    investments_path: Path | None = None  # None or missing: no investment accounts
    creditors_path: Path | None = None  # None or missing: no debt accounts
    work_path: Path | None = None  # missing: no work history yet (created when adding a month)
    credit_cards_path: Path | None = None  # None or missing: no credit card accounts
    currency: str = "$"

    @classmethod
    def from_env(cls) -> Settings:
        """Read paths from PLEDGER_* env vars, defaulting to the info/ folder."""
        return cls(
            accounts_path=Path(os.environ.get("PLEDGER_ACCOUNTS", "info/accounts.yaml")),
            base_movements_path=Path(
                os.environ.get("PLEDGER_BASE_MOVEMENTS", "info/result_types.yaml")
            ),
            ledger_path=Path(os.environ.get("PLEDGER_LEDGER", "info/ledger.csv")),
            currencies_path=Path(os.environ.get("PLEDGER_CURRENCIES", "info/currencies.yaml")),
            investments_path=Path(os.environ.get("PLEDGER_INVESTMENTS", "info/investment.yaml")),
            creditors_path=Path(os.environ.get("PLEDGER_CREDITORS", "info/creditors.yaml")),
            work_path=Path(os.environ.get("PLEDGER_WORK", "info/work.csv")),
            credit_cards_path=Path(os.environ.get("PLEDGER_CREDIT_CARDS", "info/credit_cards.yaml")),
            currency=os.environ.get("PLEDGER_CURRENCY", "$"),
        )


@dataclass
class Snapshot:
    """Everything loaded from disk for one request."""

    accounts: dict[str, Account]
    base_movements: dict[str, MovementBase]
    result_types: dict[int, ResultType]
    rows: DataFrame  # raw CSV rows, all columns as strings
    currencies: dict[str, Currency]  # the base currency first
    # Generated initial movements, like CSV rows, indexed -1, -2, ...
    initial_rows: DataFrame

    @property
    def base_currency(self) -> Currency:
        return next(iter(self.currencies.values()))

    @property
    def all_rows(self) -> DataFrame:
        """The initial movements of the accounts, then the CSV rows."""
        if not len(self.rows):  # concat fails on an empty, header-only CSV
            return self.initial_rows
        return concat([self.initial_rows, self.rows])

    def ledger(self, rows: DataFrame | None = None) -> Ledger:
        """Build a Ledger from these rows (all rows, initial ones included, by default)."""
        return Ledger.from_dataframe(
            self.all_rows if rows is None else rows,
            self.accounts,
            self.base_movements,
        )

    def free_accounts(self) -> list[Account]:
        """Accounts that can be picked on a non-fixed side of a movement."""
        return [a for a in self.accounts.values() if a.acc_type != AccountType.EQUITY]

    def side_accounts(self, base: MovementBase, ledger_side: LedgerSide) -> list[Account]:
        """Accounts that can be picked on a non-fixed side of a base movement."""
        special = SPECIAL_SIDES.get(base.side(ledger_side))
        if special:
            return [a for a in self.accounts.values() if isinstance(a, special)]
        return self.free_accounts()

    @property
    def initial_base_names(self) -> set[str]:
        """
        Base movements of the result type of the initial movements, left out of
        results by default: they set opening balances, they aren't gains or losses.
        """
        initial_rt = self.base_movements[INITIAL_GAIN].result_type
        return {n for n, b in self.base_movements.items() if b.result_type is initial_rt}

    def debt_accounts(self, month: str = "") -> list[DebtAccount]:
        """
        Copies of the debt accounts as of the end of `month` (all the ledger by default):
        their initial amounts, once they start, plus the CSV movements up to then.
        """
        debts = {}
        for a in self.accounts.values():
            if isinstance(a, DebtAccount):
                started = not month or a.initial_date is None or a.initial_date.isoformat()[:7] <= month
                creditors = {n: replace(c, amount=c.amount if started else 0.0) for n, c in a.creditors.items()}
                debts[a.name] = replace(a, creditors=creditors)
        rows = self.rows[self.rows.move_date.str[:7] <= month] if month else self.rows
        _apply_debts({**self.accounts, **debts}, rows)
        return list(debts.values())

    def manual_base_movements(self) -> dict[str, MovementBase]:
        """Base movements that can be picked for a new movement (not internal ones)."""
        return {n: b for n, b in self.base_movements.items() if not b.internal}


# Columns of the work CSV; the received payment date one is optional.
MONTH, COMPANY, POSITION, GROSS, NET, PAYMENT_DATE = (
    "Month", "Company", "Position", "Gross salary", "Net salary", "Received payment date",
)
WORK_COLUMNS = [MONTH, COMPANY, POSITION, GROSS, NET, PAYMENT_DATE]


@dataclass
class Work:
    """The work history, one row per month, loaded from disk for one request."""

    months: list[WorkMonth]  # in order, with no missing months
    rows: DataFrame  # raw CSV rows, all columns as strings

    def __post_init__(self):
        self.companies: dict[str, Company] = Company.from_months(self.months)

    @property
    def current(self) -> WorkMonth | None:
        """The last month, if it is not a gap: the current job."""
        last = self.months[-1] if self.months else None
        return None if last is None or last.is_gap else last

    def position_of(self, month: WorkMonth) -> JobPosition | None:
        return None if month.is_gap else self.companies[month.company].positions[month.position]


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
            base_currency, other_currencies = Currency.load_yaml(str(s.currencies_path))
            currencies = {base_currency.name: base_currency, **other_currencies}
            accounts = Account.load_yaml(
                str(s.accounts_path), base_currency, other_currencies,
                _existing(s.investments_path), _existing(s.creditors_path),
                list(self.load_work().companies), _existing(s.credit_cards_path),
            )
            base_movements, result_types = MovementBase.load_yaml(
                str(s.base_movements_path), accounts
            )
            _check_initial_base_movements(base_movements)
            rows = read_csv(
                s.ledger_path, sep=";", dtype=str, keep_default_na=False,
                encoding="utf-8-sig",
            )
            stored = sorted(set(rows["base_movement"]) & {INITIAL_GAIN, INITIAL_LOSS})
            if stored:
                raise ValueError(
                    f"The ledger CSV cannot have movements of {stored}: they are generated "
                    "from the initial amounts of the accounts"
                )
            initial_rows = _initial_rows(accounts, base_movements, rows)
            snapshot = Snapshot(
                accounts, base_movements, result_types, rows, currencies, initial_rows
            )
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
        if snapshot.base_movements[base_movement].internal:
            raise ValueError(f"Base movement {base_movement!r} is internal and cannot be added by hand")
        for name in (acc_debit, acc_credit):
            if name not in snapshot.accounts:
                raise ValueError(f"Unknown account: {name!r}")
        value = _parse_amount(
            amount, "amount",
            "The amount cannot be negative; pick the opposite base movement or swap the accounts",
        )
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
        csv_file = self._read_file(len(snapshot.rows), self.settings.ledger_path)
        csv_file.lines.append(buf.getvalue())
        self._write_file(csv_file)

    def add_account(
        self,
        name: str,
        acc_type: str,
        description: str,
        initial_amount: str | float,
        currency: str = "",
        is_liquid: bool = False,
        initial_date: str = "",
    ) -> None:
        """
        Validate a new account and add it to the accounts YAML. Without a currency,
        it is in the base currency; without an initial date, it starts on the
        first date of the ledger. Its initial movement is generated when loading.
        """
        snapshot = self.load()
        name = _check_name(name, snapshot.accounts, "account")
        if name in SPECIAL_SIDES:
            raise ValueError(f"{name!r} is reserved and cannot be an account name")
        try:
            acc_type = AccountType(acc_type)
        except ValueError:
            raise ValueError(f"Invalid account type: {acc_type!r}") from None
        if acc_type == AccountType.EQUITY:
            raise ValueError("New equity accounts are not supported")
        description = _check_description(description, "account")
        currency = currency.strip() or snapshot.base_currency.name
        if currency not in snapshot.currencies:
            raise ValueError(f"Unknown currency: {currency!r}")
        if not str(initial_amount).strip():
            raise ValueError("The initial amount is required")
        amount = _parse_amount(initial_amount, "initial amount")
        fields = {
            "description": description,
            "currency": currency,
            "is_liquid": bool(is_liquid),
            "initial_amount": amount,
        }
        if initial_date.strip():
            try:
                fields["initial_date"] = dt.date.fromisoformat(initial_date.strip())
            except ValueError:
                raise ValueError(f"Invalid initial date: {initial_date!r}") from None
        self._add_yaml_entry(self.settings.accounts_path, (acc_type.value,), name, fields)

    def add_result_type(self, name: str, long_name: str, description: str) -> None:
        """Validate a new result type and add it, with the next id, to the base movements YAML."""
        snapshot = self.load()
        existing = {rt.name: rt for rt in snapshot.result_types.values()}
        name = _check_name(name, existing, "result type")
        # The name is a YAML key that later base movements are inserted under.
        if not yaml.safe_dump({name: 0}).startswith(f"{name}:"):
            raise ValueError(f"The result type name {name!r} needs quoting in YAML; use a simpler name")
        long_name = long_name.strip()
        if not long_name:
            raise ValueError("The result type long name is required")
        description = _check_description(description, "result type")
        fields = {
            "id": max(snapshot.result_types, default=0) + 1,
            "long_name": long_name,
            "description": description,
        }
        self._add_yaml_entry(self.settings.base_movements_path, ("result_types",), name, fields)

    def add_base_movement(
        self,
        name: str,
        move_type: str,
        description: str,
        debit: str = "",
        credit: str = "",
        result_type: str = "",
    ) -> None:
        """
        Validate a new base movement and add it to the base movements YAML: gains
        and losses under their result type, transfers in the `transfer` section.
        A side set to INVESTMENT or DEBT takes any investment or debt account.
        """
        snapshot = self.load()
        name = _check_name(name, snapshot.base_movements, "base movement")
        try:
            move_type = MovementType(move_type)
        except ValueError:
            raise ValueError(f"Invalid movement type: {move_type!r}") from None
        description = _check_description(description, "base movement")
        if move_type == MovementType.TRANSFER:
            section = (MovementType.TRANSFER.value,)
        else:
            result_types = {rt.name: rt for rt in snapshot.result_types.values()}
            if not result_type:
                raise ValueError("Gains and losses require a result type")
            if result_type not in result_types:
                raise ValueError(f"Unknown result type: {result_type!r}")
            if result_types[result_type].internal:
                raise ValueError(f"Result type {result_type!r} is internal and cannot get new base movements")
            section = ("result_types", result_type, "base_movements", move_type.value)
        # Gains always credit equity and losses always debit it.
        if move_type == MovementType.GAIN:
            credit = ""
        elif move_type == MovementType.LOSS:
            debit = ""
        free = {a.name for a in snapshot.free_accounts()}
        for acc in (debit, credit):
            if acc and acc not in SPECIAL_SIDES and acc not in free:
                raise ValueError(f"Unknown or non-selectable account: {acc!r}")
        if debit and debit not in SPECIAL_SIDES and debit == credit:
            raise ValueError("Debit and credit accounts must be different")

        fields = {"description": description}
        if debit:
            fields["debit"] = debit
        if credit:
            fields["credit"] = credit
        self._add_yaml_entry(self.settings.base_movements_path, section, name, fields)

    def _add_yaml_entry(
        self, path: Path, section: tuple[str, ...], name: str, fields: dict
    ) -> None:
        """
        Insert `name: fields` at the end of a (nested) section, as text, so the
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
            months = [_work_month(row) for _, row in rows.iterrows()]
            WorkMonth.check_sequence(months)
        except (OSError, KeyError, ValueError) as e:
            raise LedgerDataError(f"{type(e).__name__}: {e}") from e
        return Work(months, rows)

    def save_month(
        self,
        month: str,
        company: str = "",
        position: str = "",
        gross: str | float = "",
        net: str | float = "",
        payment_date: str = "",
    ) -> bool:
        """
        Save a month of the work history: replacing it if it is already there, else
        adding it right after the last month or right before the first one, so there
        are no missing months. Without a company, it is a gap (no job). Returns
        whether the month is new. Creates the work CSV if missing.
        """
        path = self.settings.work_path
        if path is None:
            raise ValueError("No work CSV is set")
        work = self.load_work()
        texts = {"company": company, "position": position}
        for what, text in texts.items():
            if any(c in str(text) for c in "\r\n"):
                raise ValueError(f"The {what} cannot contain line breaks")
        entry = WorkMonth(
            month=parse_month(month, "month"),
            company=str(company).strip() or None,
            position=str(position).strip() or None,
            gross=_parse_amount(gross, "gross salary") if str(gross).strip() else None,
            net=_parse_amount(net, "net salary") if str(net).strip() else None,
            payment_date=_parse_date(payment_date, "received payment date"),
        )

        if not _existing(path) or not path.read_bytes().strip():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(";".join(WORK_COLUMNS) + "\n", encoding="utf-8")
            work = self.load_work()
        months = [m.month for m in work.months]
        if entry.month in months:
            index = months.index(entry.month)
            old_row = dict(work.rows.iloc[index])
        elif not months or entry.month == next_month(months[-1]):
            index, old_row = len(months), {}
        elif next_month(entry.month) == months[0]:
            index, old_row = 0, {}
        else:
            expected = f"{next_month(months[-1])} (after the last month)"
            raise ValueError(
                f"Months cannot be skipped: add {expected} first, leaving the company "
                "empty for months without a job"
            )

        before = path.read_bytes().decode("utf-8")
        csv_file = self._read_file(len(work.rows), path)
        columns = list(work.rows.columns)
        if entry.payment_date and PAYMENT_DATE not in columns:
            # Add the optional column, empty for every other month.
            columns.append(PAYMENT_DATE)
            csv_file.lines[0] += ";" + PAYMENT_DATE
            for i in csv_file.row_lines:
                csv_file.lines[i] += ";"
        new_row = {col: old_row.get(col, "") for col in columns}  # keeps any other columns
        new_row.update({
            MONTH: entry.month,
            COMPANY: entry.company or "",
            POSITION: entry.position or "",
            GROSS: "" if entry.gross is None else f"{entry.gross:.2f}",
            NET: "" if entry.net is None else f"{entry.net:.2f}",
        })
        if PAYMENT_DATE in columns:
            new_row[PAYMENT_DATE] = entry.payment_date.isoformat() if entry.payment_date else ""
        buf = io.StringIO()
        csv.writer(buf, delimiter=";", lineterminator="").writerow(new_row.values())
        if old_row:
            csv_file.lines[csv_file.row_lines[index]] = buf.getvalue()
        elif index == 0 and months:
            csv_file.lines.insert(csv_file.row_lines[0], buf.getvalue())
        else:
            csv_file.lines.append(buf.getvalue())
        self._write_file(csv_file)
        try:
            self.load_work()
        except LedgerDataError as e:
            _atomic_write(path, before)
            raise ValueError(f"Could not save {entry.month}: {e}") from None
        return not old_row

    def delete_month(self, index: int, fingerprint: str | None = None) -> None:
        """
        Delete the first or the last month of the work history by position, optionally
        checking it is the expected one. Other months can only be made gaps.
        """
        rows = self.load_work().rows
        if not 0 <= index < len(rows):
            raise StaleRowError(f"Work row {index} does not exist")
        if fingerprint is not None and work_fingerprint(rows.iloc[index]) != fingerprint:
            raise StaleRowError("The work history changed since this page was loaded; reload and try again")
        if index not in {0, len(rows) - 1}:
            raise ValueError(
                "Only the first or last month can be deleted; leave the company of the others "
                "empty to make them months without a job"
            )
        csv_file = self._read_file(len(rows), self.settings.work_path)
        del csv_file.lines[csv_file.row_lines[index]]
        self._write_file(csv_file)

    def delete(self, index: int, fingerprint: str | None = None) -> None:
        """
        Delete a row by position, optionally checking it is the expected one.
        Movements of internal base movements cannot be deleted.
        """
        snapshot = self.load()
        rows = snapshot.rows
        if not 0 <= index < len(rows):
            raise StaleRowError(f"Row {index} does not exist")
        if fingerprint is not None and row_fingerprint(rows.iloc[index]) != fingerprint:
            raise StaleRowError(
                "The ledger changed since this page was loaded; reload and try again"
            )
        base_name = rows.iloc[index]["base_movement"]
        if snapshot.base_movements[base_name].internal:
            raise ValueError(f"Movements of the internal base movement {base_name!r} cannot be deleted")
        csv_file = self._read_file(len(rows), self.settings.ledger_path)
        del csv_file.lines[csv_file.row_lines[index]]
        self._write_file(csv_file)

    def _read_file(self, n_rows: int, path: Path) -> _CsvFile:
        """
        Split a CSV into raw lines, so edits touch only the affected line and
        every other byte (quoting, BOM, line endings) is kept as is.
        """
        raw = path.read_bytes().decode("utf-8")
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


def work_fingerprint(row) -> str:
    """Identifies a work CSV row's content, like row_fingerprint."""
    return "|".join(str(row.get(c, "")) for c in WORK_COLUMNS)


def _work_month(row) -> WorkMonth:
    """A WorkMonth from a work CSV row (all strings, empty for missing values)."""
    month = row[MONTH]
    return WorkMonth(
        month=month,
        company=row[COMPANY].strip() or None,
        position=row[POSITION].strip() or None,
        gross=_parse_amount(row[GROSS], f"gross salary of {month}") if row[GROSS].strip() else None,
        net=_parse_amount(row[NET], f"net salary of {month}") if row[NET].strip() else None,
        payment_date=_parse_date(row.get(PAYMENT_DATE, ""), f"received payment date of {month}"),
    )


def _check_initial_base_movements(base_movements: dict[str, MovementBase]) -> None:
    """
    INITIAL_GAIN and INITIAL_LOSS must be an internal gain and loss, with a free
    asset or liability side, to record the initial amount of the accounts.
    """
    for name, move_type, side in (
        (INITIAL_GAIN, MovementType.GAIN, LedgerSide.DEBIT),
        (INITIAL_LOSS, MovementType.LOSS, LedgerSide.CREDIT),
    ):
        base = base_movements.get(name)
        if base is None or not base.internal or base.move_type != move_type or base.side(side) is not None:
            raise ValueError(
                f"The base movement {name!r} is required: an internal {move_type.value} "
                f"with no fixed {side.value} account"
            )


def _initial_rows(
    accounts: dict[str, Account], base_movements: dict[str, MovementBase], rows: DataFrame
) -> DataFrame:
    """
    The movement recording the initial amount of each account, in definition order,
    as CSV rows: on its initial date, or the first date of the ledger (the first
    initial date if the ledger is empty, else today). Increases are INITIAL_GAIN and
    decreases INITIAL_LOSS movements against the equity account.
    """
    dates = [a.initial_date.isoformat() for a in accounts.values() if a.initial_date]
    first_date = (
        min(rows["move_date"]) if len(rows) else min(dates, default=dt.date.today().isoformat())
    )
    new_rows = []
    for acc in accounts.values():
        amount = acc.initial_amount
        if acc.acc_type == AccountType.EQUITY or math.isclose(amount, 0.0, abs_tol=1e-9):
            continue
        # A positive liability is a decrease of equity, like a negative asset.
        if (amount > 0) == (acc.acc_type == AccountType.ASSET):
            base = base_movements[INITIAL_GAIN]
            acc_debit, acc_credit = acc.name, base.acc_credit.name
        else:
            base = base_movements[INITIAL_LOSS]
            acc_debit, acc_credit = base.acc_debit.name, acc.name
        new_rows.append({
            "move_date": acc.initial_date.isoformat() if acc.initial_date else first_date,
            "base_movement": base.name,
            "acc_debit": acc_debit,
            "acc_credit": acc_credit,
            "amount": repr(abs(amount)),
        })
    return DataFrame(
        [{col: "" for col in rows.columns} | row for row in new_rows],
        columns=rows.columns,
        index=range(-1, -len(new_rows) - 1, -1),
        dtype=str,
    )


def _apply_debts(accounts: dict[str, Account], rows: DataFrame) -> None:
    """
    Add the CSV movements of the debt accounts to whoever is named in their comments:
    a credit is more that we owe them, a debit less. The initial movements are left
    out, as the initial amounts already come from the creditors YAML.
    """
    comments = rows["comment"] if "comment" in rows.columns else [""] * len(rows)
    for debit, credit, amount, comment in zip(rows["acc_debit"], rows["acc_credit"], rows["amount"], comments):
        for name, sign in ((debit, -1), (credit, 1)):
            acc = accounts[name]
            if isinstance(acc, DebtAccount):
                value = float(amount.replace(",", "").replace(" ", "") or 0)
                acc.add_debt(acc.creditor_in(comment), sign * value, comment)


def _existing(path: Path | None) -> str | None:
    """The path of an optional file, if it exists."""
    return str(path) if path is not None and path.exists() else None


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


def _parse_date(value: str, what: str) -> dt.date | None:
    """An optional ISO date: None if empty."""
    value = str(value).strip()
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Invalid {what}: {value!r}") from None


def _parse_amount(amount: str | float, what: str, negative_error: str | None = None) -> float:
    """A finite, non-negative amount, from a number or text like '1,234.50'."""
    try:
        value = float(str(amount).replace(",", "").strip())
    except ValueError:
        raise ValueError(f"Invalid {what}: {amount!r}") from None
    if not math.isfinite(value):
        raise ValueError(f"Invalid {what}: {amount!r}")
    if value < 0:
        raise ValueError(negative_error or f"The {what} cannot be negative")
    return value


def _check_name(name: str, existing: dict, what: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError(f"The {what} name is required")
    if any(c in name for c in ";\r\n"):
        raise ValueError(f"The {what} name cannot contain ';' or line breaks")
    if name in existing:
        raise ValueError(f"{what.capitalize()} {name!r} already exists")
    return name


def _check_description(description: str, what: str) -> str:
    description = description.strip()
    if not description:
        raise ValueError(f"The {what} description is required")
    return description


def _insert_yaml_entry(text: str, section: tuple[str, ...], name: str, fields: dict) -> str:
    """
    Return `text` with `name: fields` appended to the mapping at `section`, a path
    of keys from the top level (e.g. `("result_types", "Op", "base_movements", "loss")`).
    Missing keys along the path are created at the end of their parent.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)

    def is_content(line: str) -> bool:
        return bool(line.strip()) and not line.lstrip().startswith("#")

    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip())

    # The block of the current key: its header line, indent and child lines [lo, hi).
    head, head_indent, lo, hi = None, -1, 0, len(lines)
    step = None  # indentation step, taken from the file
    for depth, key in enumerate(section):
        children = [i for i in range(lo, hi) if is_content(lines[i])]
        child_indent = indent_of(lines[children[0]]) if children else None
        if child_indent is not None and head is not None:
            step = step or child_indent - head_indent
        if child_indent is None:
            child_indent = head_indent + (step or 2) if head is not None else 0

        header = re.compile(rf"^ {{{child_indent}}}{re.escape(key)}:\s*(#.*)?$")
        found = next((i for i in children if header.match(lines[i])), None)
        if found is None:
            if any(lines[i].lstrip().startswith(f"{key}:") and indent_of(lines[i]) == child_indent
                   for i in children):
                raise ValueError(f"Cannot add to the {key!r} section; edit the file by hand")
            # Append the missing key (and the rest of the path) after the block.
            at = (children[-1] + 1) if children else lo
            if head is None:
                while lines and not lines[-1].strip():
                    lines.pop()
                at = len(lines)
            unit = step or 2
            new = [
                " " * (child_indent + unit * j) + f"{k}:"
                for j, k in enumerate(section[depth:])
            ]
            entry_indent = child_indent + unit * len(new)
            new += _yaml_entry(name, fields, entry_indent, unit)
            lines[at:at] = new + ([""] if head is None else [])
            return newline.join(lines)

        head, head_indent = found, child_indent
        lo = found + 1
        hi = next(
            (i for i in range(lo, hi) if is_content(lines[i]) and indent_of(lines[i]) <= head_indent),
            hi,
        )

    # Insert after the last content line of the section.
    children = [i for i in range(lo, hi) if is_content(lines[i])]
    if children:
        entry_indent = indent_of(lines[children[0]])
        step = step or entry_indent - head_indent
        at = children[-1] + 1
    else:
        step = step or 2
        entry_indent = head_indent + step
        at = head + 1
    lines[at:at] = _yaml_entry(name, fields, entry_indent, step)
    return newline.join(lines)


def _yaml_entry(name: str, fields: dict, indent: int, step: int) -> list[str]:
    dumped = yaml.safe_dump(
        {name: fields}, sort_keys=False, allow_unicode=True,
        default_flow_style=False, indent=step, width=10**9,
    )
    return [" " * indent + line for line in dumped.splitlines()]


@dataclass
class _CsvFile:
    path: Path
    lines: list[str]
    row_lines: list[int]  # line number of each data row
    newline: str
    bom: bool
    trailing: bool
