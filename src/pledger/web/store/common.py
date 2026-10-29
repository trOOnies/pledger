"""common.py: what the modules of the store share: its errors, today, and reading, parsing and writing rows."""

from __future__ import annotations

import datetime as dt
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pandas import DataFrame, Timestamp, concat, read_csv

from pledger.enums import LedgerSide, MovementType
from pledger.models import (
    Account,
    Currency,
    DebtAccount,
    InvestmentAccount,
    MovementBase,
)
from pledger.web.store.constants import (
    BASE_AMOUNT,
    ID_COLUMN,
    INTEREST_ACCOUNT,
    OTHER_AMOUNT,
)


class LedgerDataError(Exception):
    """The ledger files could not be loaded or are inconsistent."""


class StaleRowError(Exception):
    """The row to change no longer matches what the client saw."""


def row_fingerprint(row) -> str:
    """Identifies a row's content, to detect edits made after a page was rendered."""
    return f"{row['move_date']}|{row['base_movement']}|{row['amount']}"


def conversion_fingerprint(row) -> str:
    """Identifies a conversions CSV row's content, like row_fingerprint."""
    return f"{row['move_date']}|{row['base_movement']}|{row[BASE_AMOUNT]}|{row[OTHER_AMOUNT]}"


def interest_fingerprint(row) -> str:
    """Identifies an interest CSV row's content, like row_fingerprint."""
    return f"{row['move_date']}|{row[INTEREST_ACCOUNT]}|{row['amount']}"


def stored_ids(rows: DataFrame, prefix: str) -> list[str | None]:
    """
    The id of each row of a CSV of movements, with this prefix: None if it is
    missing, malformed or the same as one of a row before it.
    """
    values = rows[ID_COLUMN] if ID_COLUMN in rows.columns else [""] * len(rows)
    ids, seen = [], set()
    for value in values:
        value = str(value).strip()
        ids.append(value if re.fullmatch(rf"{prefix}-[1-9]\d*", value) and value not in seen else None)
        seen.add(value)
    return ids


def next_id(rows: DataFrame, prefix: str) -> str:
    """A new id for a row of a CSV of movements: the next number after the highest one."""
    numbers = [int(i.split("-")[1]) for i in stored_ids(rows, prefix) if i]
    return f"{prefix}-{max(numbers, default=0) + 1}"


def work_fingerprint(row) -> str:
    """Identifies a work CSV row's content, like row_fingerprint."""
    return "|".join(str(v) for v in row.values)


def _with_row(rows: DataFrame, index: int, new_row: dict) -> DataFrame:
    """These rows with the values of `new_row` in the one at this position, but its id."""
    changed = rows.copy()
    for col, value in new_row.items():
        if col in changed.columns and col != ID_COLUMN:
            changed.loc[rows.index[index], col] = value
    return changed


def _amount_of(text: str) -> float:
    """An amount of a CSV row already validated, like 1,234.50."""
    return round(float(str(text).replace(",", "").strip() or 0), 2)


def _required_base(
    base_movements: dict[str, MovementBase], name: str, gain: bool, purpose: str = "", special: str | None = None,
) -> MovementBase:
    """
    A base movement pledger generates or handles movements of (for a `purpose`, if
    given): an internal gain with a free debit side, or an internal loss with a free
    credit side, against the equity account. With a `special` side (see SPECIAL_SIDES),
    that side may take it too.
    """
    move_type, side = (MovementType.GAIN, LedgerSide.DEBIT) if gain else (MovementType.LOSS, LedgerSide.CREDIT)
    base = base_movements.get(name)
    if base is None or not base.internal or base.move_type != move_type or base.side(side) not in {None, special}:
        takes = f"no fixed {side.value} account" + (f" (or {side.value}: {special})" if special else "")
        raise ValueError(
            f"The base movement {name!r} is required{' ' + purpose if purpose else ''}: "
            f"an internal {move_type.value} with {takes}"
        )
    return base


def _load_movements(path: Path | None, columns: list[str], what: str) -> DataFrame:
    """
    The rows of a CSV of movements, all columns as strings, which must have these
    columns but the comment; none if it is missing.
    """
    if not _existing(path) or not path.read_bytes().strip():
        return DataFrame(columns=columns, dtype=str)
    rows = read_csv(path, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    missing = [c for c in columns if c != "comment" and c not in rows.columns]
    if missing:
        raise ValueError(f"The {what} CSV is missing the columns {missing}")
    return rows


def _fit_row(row: dict, columns, what: str) -> dict:
    """A new row of a CSV with these columns, the missing ones empty. Its values must have a column."""
    lost = [col for col, value in row.items() if value and col not in columns]
    if lost:
        raise ValueError(f"The {what} CSV has no {lost[0]!r} column")
    return {col: row.get(col, "") for col in columns}


def own_initial_amount(account: Account) -> bool:
    """
    Whether the initial amount of an account is in another currency: that of the
    accounts in other currencies but the investment and debt ones, whose initial
    amounts (those of their investments or creditors) are in the base currency.
    """
    return not account.currency.is_base_currency and not isinstance(account, (InvestmentAccount, DebtAccount))


def _value_on(currency: Currency, date: str, purpose: str) -> float:
    """The value of a currency in the base one on a date: the last one up to then, else its first one."""
    values = currency.base_currency_mapping
    if values is None:
        return 1.0
    if not len(values):
        raise ValueError(f"There are no values of {currency.name} {purpose}")
    up_to = values[values.index <= Timestamp(date)]
    return float(up_to.iloc[-1] if len(up_to) else values.iloc[0])


def _generated_rows(new_rows: list[dict], columns, start: int) -> DataFrame:
    """Generated movements as CSV rows with these columns and their id, indexed from `start` down."""
    columns = [*columns, *([] if ID_COLUMN in columns else [ID_COLUMN])]
    return DataFrame(
        [{col: "" for col in columns} | row for row in new_rows],
        columns=columns,
        index=range(start, start - len(new_rows), -1),
        dtype=str,
    )


def _concat(frames: list[DataFrame], columns) -> DataFrame:
    """The rows of these frames, with these columns: concat fails on empty ones."""
    frames = [f for f in frames if len(f)]
    return concat(frames) if frames else DataFrame(columns=columns, dtype=str)


def today() -> dt.date:
    """Today, up to which the services are owed (see _service_rows)."""
    return dt.date.today()


def _check_free_sides(base: MovementBase, acc_debit: Account, acc_credit: Account) -> None:
    """
    A free side of an investment base movement (e.g. what pays for an investment
    bought, or gets its dividends or pays its fees) is never an investment account:
    only the INVESTMENT sides change the investments.
    """
    for ledger_side, acc in ((LedgerSide.DEBIT, acc_debit), (LedgerSide.CREDIT, acc_credit)):
        if base.side(ledger_side) is None and isinstance(acc, InvestmentAccount):
            raise ValueError(
                f"The {ledger_side.value} account of {base.name!r} cannot be an investment account: {acc.name}"
            )


def _quantity_text(value: float) -> str:
    """A quantity as written in the QUANTITY_COLUMN, e.g. 1,234.50 or 4.6209."""
    whole, _, decimals = f"{value:,.6f}".rstrip("0").partition(".")
    return f"{whole}.{decimals.ljust(2, '0')}"


def _header(columns: list[str]) -> str:
    """A CSV with just this header."""
    return ";".join(columns) + "\n"


def _existing(path: Path | None) -> str | None:
    """The path of an optional file, if it exists."""
    return str(path) if path is not None and path.exists() else None


def _atomic_write(path: Path, text: str) -> None:
    """Atomically replace a file, keeping its permissions, or create it (and its folder)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o644)
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


@dataclass
class _CsvFile:
    path: Path
    lines: list[str]
    row_lines: list[int]  # line number of each data row
    newline: str
    bom: bool
    trailing: bool
