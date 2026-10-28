"""store.py"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import math
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path

import yaml
from pandas import DataFrame, Series, Timestamp, concat, read_csv

from pledger.enums import (
    AccountCheck,
    AccountType,
    CreditorKind,
    InvestmentType,
    LedgerSide,
    MovementType,
)
from pledger.ledger import Ledger
from pledger.models import (
    CREDIT_CARD,
    DEBT_ACCOUNT,
    FOREIGN_CURRENCY,
    INFLATION_COLUMN,
    INTERMEDIARY,
    INTERNAL,
    INVESTMENT,
    SPECIAL_SIDES,
    Account,
    Company,
    CreditCardAccount,
    Currency,
    DebtAccount,
    Investment,
    InvestmentAccount,
    JobPosition,
    Movement,
    MovementBase,
    ResultType,
    WorkMonth,
    initial_week,
    load_previous_month,
    next_month,
    parse_month,
)
from pledger.weeks import Week, prorate_by_week

BOM = "﻿"

# Internal base movements recording the initial amount of the accounts. Their
# movements are generated from the accounts YAML, never stored in the CSV.
INITIAL_GAIN = "Initial (G)"
INITIAL_LOSS = "Initial (L)"
# Internal base movements of the result of holding accounts in other currencies:
# the change of their value in the base currency. Their movements are generated
# when loading (see _result_rows), never stored in the CSV.
RESULT_GAIN = "Foreign currency (G)"
RESULT_LOSS = "Foreign currency (L)"
# Base movement of the salary of the work history, and the column of its gross salary.
# Its movements are generated each week from the gross salary of its month (see
# _salary_rows), never stored in the CSV, like those of the deductions of the salary.
SALARY = "Work - Salary Main"
GENERATED = [INITIAL_GAIN, INITIAL_LOSS, RESULT_GAIN, RESULT_LOSS, SALARY]

# The id of each movement of the ledger: unique and unchanging. The rows of the CSVs of
# movements keep theirs in an ID_COLUMN, the prefix of their CSV and a number, given by
# pledger itself (see LedgerStore.freeze_ids); the generated movements get theirs from
# what they are generated from, so they never change either.
ID_COLUMN = "id"
LEDGER_ID, CONVERSION_ID, INVESTMENT_ID, INTEREST_ID = "L", "C", "I", "M"
INITIAL_ID, SALARY_ID, RESULT_ID = "A", "W", "R"

# Columns of the ledger CSV.
LEDGER_CSV_COLUMNS = ["move_date", "base_movement", "acc_debit", "amount", "acc_credit", "comment"]

# Columns of the conversions CSV, of the movements between an account in the base
# currency and one in another currency: the amount in each currency. Its movements
# are added to the ledger with the amount in the base currency.
BASE_AMOUNT, OTHER_AMOUNT = "amount_base_currency", "amount_other_currency"
CONVERSION_COLUMNS = ["move_date", "base_movement", "acc_debit", BASE_AMOUNT, "acc_credit", "comment", OTHER_AMOUNT]

# The internal transfers of the conversions of every other currency: bringing it in
# (debiting an account in it) or taking it out (crediting one).
CONVERSION_BOUGHT, CONVERSION_SOLD = "Foreign currency - Bought", "Foreign currency - Sold"
CONVERSIONS = {CONVERSION_BOUGHT, CONVERSION_SOLD}
# A cross conversion, between two other currencies: a CONVERSION_SOLD of one into an
# account in the base currency, right followed by a CONVERSION_BOUGHT of the other out
# of it (see conversion_pairs).
CROSS_CONVERSION = f"{CONVERSION_SOLD} + {CONVERSION_BOUGHT}"

# The internal transfers of the debt account, added from the Debt tab, with that side
# fixed to it (DEBT_ACCOUNT): we owe more or less, or are owed more or less.
DEBT_INCURRED, DEBT_PAID = "Debt - Incurred", "Debt - Paid"
LOAN_GIVEN, LOAN_COLLECTED = "Loan - Given", "Loan - Collected"
DEBT_BASES = {
    DEBT_INCURRED: LedgerSide.CREDIT, DEBT_PAID: LedgerSide.DEBIT,
    LOAN_GIVEN: LedgerSide.DEBIT, LOAN_COLLECTED: LedgerSide.CREDIT,
}
# The internal transfers paying a credit card (debiting it) and getting back a surplus of
# it (a negative payment, crediting it), added from the Credit Cards tab, with that side
# taking any credit card account (CREDIT_CARD).
CARD_PAYMENT, CARD_SURPLUS = "Credit card - Payment", "Credit card - Surplus"
CARD_PAYMENT_BASES = {CARD_PAYMENT: LedgerSide.DEBIT, CARD_SURPLUS: LedgerSide.CREDIT}

# Internal result type of the investment results. Its base movements, and the
# internal transfers with an INVESTMENT side, are added and deleted from the
# Investments tab instead of the Ledger tab.
INVEST = "Invest"
# Investment base movements of the interest earned or paid on the money of a
# regular account, with no investment involved.
INTEREST_GAIN = "Interest on Money (G)"
INTEREST_LOSS = "Interest on Money (L)"
INTEREST = {INTEREST_GAIN, INTEREST_LOSS}
# The internal gain and loss of the corrections of the balance of an account to the one
# checked by hand, in favour of oneself or against, in the ledger CSV: added from the
# Accounts tab and deleted from the Ledger one (see LedgerStore.correct_balance)
CORRECTION_GAIN = "Correction (G)"
CORRECTION_LOSS = "Correction (L)"
CORRECTIONS = {CORRECTION_GAIN: MovementType.GAIN, CORRECTION_LOSS: MovementType.LOSS}

# Columns of the investments CSV, of the movements of the investment base movements
# but the interest on money: those of the ledger CSV, plus the investment (its name
# in the investment account) and its quantity, like 1,234.50.
INVESTMENT_COLUMN, QUANTITY_COLUMN = "investment", "quantity"
INVESTMENT_COLUMNS = [
    "move_date", "base_movement", "acc_debit", "amount", "acc_credit", QUANTITY_COLUMN, INVESTMENT_COLUMN, "comment",
]
# Columns of the interest CSV, of the interest on money of a regular account: earned
# (an INTEREST_GAIN movement debiting it) if its amount is positive, else paid (an
# INTEREST_LOSS one crediting it).
INTEREST_ACCOUNT = "acc_debit"
INTEREST_COLUMNS = ["move_date", INTEREST_ACCOUNT, "amount", "comment"]
# Investment base movements that change the quantity of the investment: it is
# required to buy, sell and split, and optional for capital repayments.
QUANTITY_REQUIRED = {"Investment - Bought", "Investment - Sold", "Stock split (More)", "Stock split (Less)"}
QUANTITY_OPTIONAL = {"Capital repayments"}


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
    conversions_path: Path | None = None  # missing: no conversions yet (created when adding one)
    investment_movements_path: Path | None = None  # missing: none yet (created when adding one)
    interest_path: Path | None = None  # missing: no interest on money yet (created when adding some)
    currency: str = "$"

    @classmethod
    def from_env(cls) -> Settings:
        """Read paths from PLEDGER_* env vars, defaulting to the info/ folder."""
        return cls(
            accounts_path=Path(os.environ.get("PLEDGER_ACCOUNTS", "info/accounts.yaml")),
            base_movements_path=Path(
                os.environ.get("PLEDGER_BASE_MOVEMENTS", "info/result_types.yaml")
            ),
            ledger_path=Path(os.environ.get("PLEDGER_LEDGER", "info/movements/ledger.csv")),
            currencies_path=Path(os.environ.get("PLEDGER_CURRENCIES", "info/currencies.yaml")),
            investments_path=Path(os.environ.get("PLEDGER_INVESTMENTS", "info/investment.yaml")),
            creditors_path=Path(os.environ.get("PLEDGER_CREDITORS", "info/creditors.yaml")),
            work_path=Path(os.environ.get("PLEDGER_WORK", "info/work.csv")),
            credit_cards_path=Path(os.environ.get("PLEDGER_CREDIT_CARDS", "info/credit_cards.yaml")),
            conversions_path=Path(os.environ.get("PLEDGER_CONVERSIONS", "info/movements/conversion.csv")),
            investment_movements_path=Path(
                os.environ.get("PLEDGER_INVESTMENT_MOVEMENTS", "info/movements/investment.csv")
            ),
            interest_path=Path(os.environ.get("PLEDGER_INTEREST", "info/movements/interest.csv")),
            currency=os.environ.get("PLEDGER_CURRENCY", "$"),
        )

    def expected_files(self) -> dict[Path, str]:
        """
        Every data file, with what it is created with when missing: the YAMLs empty,
        the CSVs with just their header.
        """
        currencies_folder = self.currencies_path.parent
        files = {
            self.accounts_path: "",
            self.base_movements_path: "",
            self.ledger_path: _header([*LEDGER_CSV_COLUMNS, ID_COLUMN]),
            self.currencies_path: "",
            currencies_folder / "base_currency.csv": _header(["month", INFLATION_COLUMN]),
            currencies_folder / "currencies.csv": _header(["date"]),
            self.investments_path: "",
            self.creditors_path: "",
            self.work_path: _header(WORK_COLUMNS),
            self.credit_cards_path: "",
            self.conversions_path: _header([*CONVERSION_COLUMNS, ID_COLUMN]),
            self.investment_movements_path: _header([*INVESTMENT_COLUMNS, ID_COLUMN]),
            self.interest_path: _header([*INTEREST_COLUMNS, ID_COLUMN]),
        }
        return {path: text for path, text in files.items() if path is not None}

    def create_missing_files(self) -> list[Path]:
        """Create the missing data files (see expected_files) and their folders. Returns those created."""
        created = []
        for path, text in self.expected_files().items():
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                created.append(path)
        return created


@dataclass
class Snapshot:
    """Everything loaded from disk for one request."""

    accounts: dict[str, Account]
    base_movements: dict[str, MovementBase]
    result_types: dict[int, ResultType]
    rows: DataFrame  # raw ledger CSV rows, all columns as strings
    currencies: dict[str, Currency]  # the base currency first
    conversions: DataFrame  # raw conversions CSV rows, all columns as strings
    investments: DataFrame  # raw investments CSV rows, all columns as strings
    interest: DataFrame  # raw interest CSV rows, all columns as strings
    work: Work
    # The top-level previous month of the accounts YAML, if any (see Account.load_yaml)
    previous_month: str | None = None
    # The first date of the ledger: the Monday of the initial week (see initial_week), else
    # the first date of the movements of the CSVs, else the first initial date of the
    # accounts, else today.
    start_date: str = field(init=False)
    # Generated movements, like ledger CSV rows, indexed -1, -2, ...: the initial ones,
    # those of the investments, interest and conversions CSVs, the salaries and the
    # results of the accounts in other currencies.
    initial_rows: DataFrame = field(init=False)
    investment_rows: DataFrame = field(init=False)
    interest_rows: DataFrame = field(init=False)
    conversion_rows: DataFrame = field(init=False)
    salary_rows: DataFrame = field(init=False)
    result_rows: DataFrame = field(init=False)
    # The balance of each account in another currency in that currency, and its movements
    # (see _result_rows)
    other_balances: dict[str, float] = field(init=False)
    currency_moves: dict[str, list[tuple]] = field(init=False)
    # The id of each movement by its index in all_rows: None for a CSV row without a
    # valid one yet (see stored_ids), until the ids are frozen again.
    ids: dict[int, str | None] = field(init=False)

    def __post_init__(self):
        dates = [
            *self.rows["move_date"], *self.investments["move_date"],
            *self.interest["move_date"], *self.conversions["move_date"],
        ]
        initial_dates = [a.initial_date.isoformat() for a in self.accounts.values() if a.initial_date]
        week = self.initial_week
        if week is not None:
            # The initial week only has the initial movements: the others come after it.
            self.start_date = week.start.isoformat()
            early = sorted({d for d in dates if d <= week.end.isoformat()})
            if early:
                raise ValueError(
                    f"There are movements up to the end of the initial week ({week.start} to {week.end}, the "
                    f"last one of the previous month {self.previous_month}), which only has the initial ones: {early}"
                )
        else:
            self.start_date = min(dates) if dates else min(initial_dates, default=dt.date.today().isoformat())
        self.initial_rows = _initial_rows(self, self.start_date)
        start = -len(self.initial_rows) - 1
        for name, generate in (
            ("investment_rows", _investment_rows),
            ("interest_rows", _interest_rows),
            ("conversion_rows", _conversion_rows),
            ("salary_rows", _salary_rows),
        ):
            setattr(self, name, generate(self, start))
            start -= len(getattr(self, name))
        self.result_rows, self.other_balances, self.currency_moves = _result_rows(self, start)
        self.ids = {}
        for generated, stored, prefix in (
            (self.rows, self.rows, LEDGER_ID),
            (self.investment_rows, self.investments, INVESTMENT_ID),
            (self.interest_rows, self.interest, INTEREST_ID),
            (self.conversion_rows, self.conversions, CONVERSION_ID),
        ):
            self.ids |= zip(generated.index, stored_ids(stored, prefix))
        for generated in (self.initial_rows, self.salary_rows, self.result_rows):
            self.ids |= zip(generated.index, generated[ID_COLUMN])

    @property
    def initial_week(self) -> Week | None:
        """The week of the initial movements, the last one of the previous month, if any (see initial_week)."""
        return initial_week(self.previous_month) if self.previous_month else None

    @property
    def base_currency(self) -> Currency:
        return next(iter(self.currencies.values()))

    @property
    def booked_rows(self) -> DataFrame:
        """
        The movements of the CSVs and the salaries: all but the initial ones and the
        results of the accounts in other currencies.
        """
        return _concat(
            [self.rows, self.investment_rows, self.interest_rows, self.conversion_rows, self.salary_rows],
            self.rows.columns,
        )

    @cached_property
    def all_rows(self) -> DataFrame:
        """
        The initial movements of the accounts, the CSV rows, the investments, the
        interest on money, the conversions, the salaries and the results of the accounts
        in other currencies, in date order (each date in that order).
        """
        rows = _concat([self.initial_rows, self.booked_rows, self.result_rows], self.rows.columns)
        return rows.assign(**{ID_COLUMN: [self.ids[ix] for ix in rows.index]}).sort_values("move_date", kind="stable")

    def own_balance(self, name: str, date: str) -> float:
        """
        The balance of an account in another currency, in its currency, at the end of
        `date` (see _result_rows).
        """
        values = self.accounts[name].currency.base_currency_mapping
        return float(sum(_in_currency(m, values) for m in self.currency_moves.get(name, []) if m[0] <= date))

    def conversion_balances(self) -> dict[int, float]:
        """
        The balance in its currency of the account in another currency of each conversion,
        by its row in the conversions CSV, right after it (see _result_rows).
        """
        after = {}
        for name, moves in self.currency_moves.items():
            values = self.accounts[name].currency.base_currency_mapping
            if not len(values):
                continue
            total = 0.0
            for move in sorted(moves, key=lambda m: m[0]):  # stable: in their order on each date
                total += _in_currency(move, values)
                if move[3] is not None:
                    after[move[3]] = total
        return after

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
        """
        Accounts that can be picked on a non-fixed side of a base movement: never an
        investment account on a free side of an investment base movement.
        """
        special = SPECIAL_SIDES.get(base.side(ledger_side))
        if special:
            return [a for a in self.accounts.values() if special(a)]
        if base.name in self.invest_base_movements():
            return [a for a in self.free_accounts() if not isinstance(a, InvestmentAccount)]
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
        their initial amounts, once they start, plus the booked movements up to then.
        """
        debts = {}
        for a in self.accounts.values():
            if isinstance(a, DebtAccount):
                started = not month or a.initial_date is None or a.initial_date.isoformat()[:7] <= month
                creditors = {n: replace(c, amount=c.amount if started else 0.0) for n, c in a.creditors.items()}
                debts[a.name] = replace(a, creditors=creditors)
        rows = self.booked_rows
        rows = rows[rows.move_date.str[:7] <= month] if month else rows
        _apply_debts({**self.accounts, **debts}, rows)
        return list(debts.values())

    def investment_accounts(
        self, unattributed: list[dict] | None = None, month: str = ""
    ) -> list[InvestmentAccount]:
        """
        Copies of the investment accounts as of the end of `month` (all the ledger by
        default), with the movements of the investments CSV up to then applied to their
        investments (see _apply_investments). Their initial amounts
        and quantities count once they start. The results that can't be attributed
        to an investment are added to `unattributed`.
        """
        def started(a: InvestmentAccount) -> bool:
            return not month or a.initial_date is None or a.initial_date.isoformat()[:7] <= month

        accounts = {
            a.name: replace(
                a,
                investments={
                    n: dict(h) if started(a) else {**h, "total_amount": 0.0, "total_quantity": 0.0}
                    for n, h in a.investments.items()
                },
                results={n: dict(r) for n, r in a.results.items()},
            )
            for a in self.accounts.values()
            if isinstance(a, InvestmentAccount)
        }
        rows = self.investments
        rows = rows[rows.move_date.str[:7] <= month] if month else rows
        found = _apply_investments(self, {**self.accounts, **accounts}, rows)
        if unattributed is not None:
            unattributed.extend(found)
        return list(accounts.values())

    def manual_base_movements(self) -> dict[str, MovementBase]:
        """Base movements that can be picked for a new movement (not internal or generated ones)."""
        return {n: b for n, b in self.base_movements.items() if not b.internal and n not in GENERATED}

    @property
    def conversion_base_names(self) -> set[str]:
        """The names of the base movements of the conversions: CONVERSION_BOUGHT and CONVERSION_SOLD."""
        return set(CONVERSIONS)

    def conversion_base_movements(self) -> dict[str, MovementBase]:
        """The base movements of the conversions, added from the Currencies tab: the CONVERSIONS that are internal transfers."""
        return {
            n: b for n in sorted(CONVERSIONS)
            if (b := self.base_movements.get(n)) is not None and b.internal and b.move_type == MovementType.TRANSFER
        }

    def debt_base_movements(self) -> dict[str, MovementBase]:
        """The base movements of DEBT_BASES, added from the Debt tab, that are as expected (see tab_base_movements)."""
        return self.tab_base_movements(DEBT_BASES, DEBT_ACCOUNT)[0]

    def card_payment_base_movements(self) -> dict[str, MovementBase]:
        """The base movements of CARD_PAYMENT_BASES, added from the Credit Cards tab, that are as expected (see tab_base_movements)."""
        return self.tab_base_movements(CARD_PAYMENT_BASES, CREDIT_CARD)[0]

    def tab_base_movements(
        self, expected: dict[str, LedgerSide], special: str
    ) -> tuple[dict[str, MovementBase], list[str]]:
        """
        The base movements of these names that are internal transfers with this
        special side (or fixed account) on their side, and the descriptions of the
        others, missing or not.
        """
        found, wrong = {}, []
        for name, side in expected.items():
            base = self.base_movements.get(name)
            if (
                base is not None and base.internal and base.move_type == MovementType.TRANSFER
                and base.side(side) == special
            ):
                found[name] = base
            else:
                wrong.append(f"{name!r} (an internal transfer with {side.value}: {special})")
        return found, wrong

    def correction_base_movements(self) -> dict[str, MovementBase]:
        """The base movements of the balance corrections (CORRECTIONS) that are internal ones of their type."""
        return {
            n: b for n, t in CORRECTIONS.items()
            if (b := self.base_movements.get(n)) is not None and b.internal and b.move_type == t
        }

    def ledger_base_movements(self) -> dict[str, MovementBase]:
        """The base movements that can be picked in the Ledger tab: the manual ones but those of other tabs."""
        other_tabs = (
            self.conversion_base_names | set(self.debt_base_movements()) | set(self.card_payment_base_movements())
        )
        return {n: b for n, b in self.manual_base_movements().items() if n not in other_tabs}

    def invest_base_movements(self) -> dict[str, MovementBase]:
        """
        The internal base movements handled in the Investments tab: those of the
        INVEST result type (but the CORRECTIONS) and the transfers with an INVESTMENT side. The movements of
        the INTEREST ones are in the interest CSV, and those of the others in the
        investments CSV.
        """
        return {
            n: b for n, b in self.base_movements.items()
            if b.internal and n not in CORRECTIONS and (
                b.result_type.name == INVEST if b.result_type is not None
                else INVESTMENT in {b.debit_special, b.credit_special}
            )
        }


# Columns of the work CSV; the received payment date one is optional. Any other column
# is a deduction of the gross salary (taxes, contributions...) on the way to the net
# one, named after its loss base movement.
MONTH, COMPANY, POSITION, GROSS, PAYMENT_DATE = "Month", "Company", "Position", SALARY, "Received payment date"
WORK_COLUMNS = [MONTH, COMPANY, POSITION, GROSS, PAYMENT_DATE]


@dataclass
class Work:
    """The work history, one row per month, loaded from disk for one request."""

    months: list[WorkMonth]  # in order, with no missing months
    rows: DataFrame  # raw CSV rows, all columns as strings

    def __post_init__(self):
        self.companies: dict[str, Company] = Company.from_months(self.months)

    @property
    def deductions(self) -> list[str]:
        """The base movements of the deductions of the salary: the other columns, in order."""
        return deduction_columns(self.rows.columns)

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


class LedgerStore:
    """Reads and writes the ledger files. The files are the source of truth."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def load(self) -> Snapshot:
        s = self.settings
        try:
            base_currency, other_currencies = Currency.load_yaml(str(s.currencies_path))
            currencies = {base_currency.name: base_currency, **other_currencies}
            work = self.load_work()
            accounts = Account.load_yaml(
                str(s.accounts_path), base_currency, other_currencies,
                _existing(s.investments_path), _existing(s.creditors_path),
                list(work.companies), _existing(s.credit_cards_path),
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
                work, load_previous_month(str(s.accounts_path)),
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
        `correction` (from the Accounts tab), else one of the Ledger tab (see _check_tab). It gets the next id of its CSV (see _appended).
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

    def add_investment(
        self, account: str, name: str, investment_type: str, amount: str | float = "", quantity: str | float = ""
    ) -> None:
        """
        Add an investment to an investment account in the investments YAML, with its
        type and, optionally, its initial amount and quantity (0 by default). An
        investment only in the ledger gets its type this way; one in the YAML already
        has it.
        """
        snapshot = self.load()
        path = self.settings.investments_path
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
        fields: str | dict = inv_type.value
        if str(amount).strip() or str(quantity).strip():
            fields = {
                "type": inv_type.value,
                "amount": _parse_amount(amount or 0, "initial amount"),
                "quantity": _parse_amount(quantity or 0, "initial quantity"),
            }
        before = path.read_bytes().decode("utf-8") if path.exists() else None
        _atomic_write(path, _insert_yaml_entry(before or "", (account,), name, fields))
        try:
            self.load()
        except LedgerDataError as e:
            if before is None:
                path.unlink()
            else:
                _atomic_write(path, before)
            raise ValueError(f"Could not add {name!r}: {e}") from None

    def add_account(
        self,
        name: str,
        acc_type: str,
        description: str,
        initial_amount: str | float,
        currency: str = "",
        is_liquid: bool = False,
        initial_date: str = "",
        checks: Sequence[str] = (),
    ) -> None:
        """
        Validate a new account and add it to the accounts YAML. Without a currency,
        it is in the base currency; without an initial date, it starts on the
        first date of the ledger. Its initial amount is in its currency. Its initial
        movement is generated when loading, and must not break its `checks`
        (AccountCheck values) either.
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
        fields = _account_fields(snapshot, description, initial_amount, currency, is_liquid, initial_date, checks)
        self._add_yaml_entry(self.settings.accounts_path, (acc_type.value,), name, fields)

    def edit_account(
        self,
        name: str,
        description: str,
        initial_amount: str | float = "",
        is_liquid: bool = False,
        initial_date: str = "",
        checks: Sequence[str] = (),
    ) -> None:
        """
        Validate the new fields of an asset or liability account, like add_account, and
        write them over its entry in the accounts YAML, or in the credit cards YAML for a
        credit card account; its name, type and currency stay, and so do the other keys of
        the entry.
        Investment and debt accounts have no initial amount (theirs is that of their
        investments or creditors), and credit card accounts are always liquid. Rolls back
        if the files no longer load.
        """
        snapshot = self.load()
        account = snapshot.accounts.get(name)
        if account is None:
            raise ValueError(f"Unknown account: {name!r}")
        if account.acc_type not in {AccountType.ASSET, AccountType.LIABILITY}:
            raise ValueError(f"Only asset and liability accounts can be edited, not {name!r}")
        fixed_amount = isinstance(account, (InvestmentAccount, DebtAccount))
        fields = _account_fields(
            snapshot, description, 0 if fixed_amount else initial_amount, account.currency.name, is_liquid,
            initial_date, checks,
        )
        if fixed_amount:
            del fields["initial_amount"]
        if isinstance(account, CreditCardAccount):
            del fields["is_liquid"]
            path, section = self.settings.credit_cards_path, ()
        else:
            path = self.settings.accounts_path
            section = (*((INTERNAL,) if account.internal else ()), account.acc_type.value)

        def update(entry: dict) -> dict:
            entry = {k: v for k, v in entry.items() if k not in {"initial_date", "checks"}}
            return {**entry, **fields}

        before = path.read_bytes().decode("utf-8")
        _atomic_write(path, _replace_yaml_entry(before, (*section, name), update))
        try:
            self.load()
        except LedgerDataError as e:
            _atomic_write(path, before)
            raise ValueError(f"Could not edit {name!r}: {e}") from None

    def correct_balance(self, name: str, balance: str | float, check_date: str = "") -> tuple[str, float] | None:
        """
        Correct the balance of an asset or liability account (but an investment one) to
        `balance`, checked by hand at the end of `check_date` (today by default), in its
        currency: a CORRECTION_GAIN or CORRECTION_LOSS movement on that date of the
        difference with its balance then, out of or into equity, in the ledger CSV. The difference in another currency is at
        the value of that currency on that date. Returns the base movement and the amount
        in the base currency, or None if the balance was already right.
        """
        snapshot = self.load()
        account = snapshot.accounts.get(name)
        if account is None or account.acc_type not in {AccountType.ASSET, AccountType.LIABILITY}:
            raise ValueError(f"Only the balances of asset and liability accounts are corrected, not {name!r}")
        if isinstance(account, InvestmentAccount):
            raise ValueError(f"The balance of the investment account {name!r} comes from its investments")  # noqa: TRY004 (a bad pick)
        if not str(balance).strip():
            raise ValueError("The checked balance is required")
        try:
            checked = float(str(balance).replace(",", "").strip())
        except ValueError:
            raise ValueError(f"Invalid checked balance: {balance!r}") from None
        if not math.isfinite(checked):
            raise ValueError(f"Invalid checked balance: {balance!r}")
        check_date = check_date.strip() or dt.date.today().isoformat()
        try:
            dt.date.fromisoformat(check_date)
        except ValueError:
            raise ValueError(f"Invalid check date: {check_date!r}") from None

        if account.currency.is_base_currency:
            rows = snapshot.all_rows
            difference = checked - snapshot.ledger(rows[rows.move_date <= check_date]).balances().get(name, 0.0)
        else:
            value = _value_on(account.currency, check_date, f"to correct {name!r} with")
            difference = (checked - snapshot.own_balance(name, check_date)) * value
        amount = round(abs(difference), 2)
        if amount < 0.005:
            return None
        gain = (difference > 0) == (account.acc_type == AccountType.ASSET)
        base_name = CORRECTION_GAIN if gain else CORRECTION_LOSS
        if base_name not in snapshot.correction_base_movements():
            raise ValueError(
                f"Correcting balances needs {base_name!r}, an internal {'gain' if gain else 'loss'} of the "
                "base movements YAML"
            )
        base = snapshot.base_movements[base_name]
        debit, credit = (name, base.acc_credit.name) if gain else (base.acc_debit.name, name)
        comment = f"Checked balance: {checked:,.2f} {account.currency.name}"
        self.add(check_date, base_name, debit, credit, amount, comment, correction=True)
        return base_name, amount

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
        A side set to one of the RESERVED names takes any such account (see SPECIAL_SIDES).
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

    def save_month(
        self,
        month: str,
        company: str = "",
        position: str = "",
        gross: str | float = "",
        deductions: dict[str, str | float] | None = None,
        payment_date: str = "",
    ) -> bool:
        """
        Save a month of the work history: replacing it if it is already there, else
        adding it right after the last month or right before the first one, so there
        are no missing months. Without a company, it is a gap (no job). The deductions
        of the salary are by the name of their loss base movement, 0 if left out; a
        new one adds its column, 0 for every other month with a job. Returns whether
        the month is new. Creates the work CSV if missing.
        """
        path = self.settings.work_path
        if path is None:
            raise ValueError("No work CSV is set")
        work = self.load_work()
        texts = {"company": company, "position": position}
        for what, text in texts.items():
            if any(c in str(text) for c in "\r\n"):
                raise ValueError(f"The {what} cannot contain line breaks")
        deductions = {str(n).strip(): v for n, v in (deductions or {}).items()}
        reserved = [n for n in deductions if n in WORK_COLUMNS or not n or any(c in n for c in "\r\n;\"")]
        if reserved:
            raise ValueError(f"Invalid deduction of the salary: {reserved[0]!r}")
        entry = WorkMonth(
            month=parse_month(month, "month"),
            company=str(company).strip() or None,
            position=str(position).strip() or None,
            gross=_parse_amount(gross, "gross salary") if str(gross).strip() else None,
            deductions={
                n: _parse_amount(v, f"deduction {n!r}") for n, v in deductions.items() if str(v).strip()
            },
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
        new_columns = [n for n in entry.deductions if n not in columns and entry.deductions[n]]
        if entry.payment_date and PAYMENT_DATE not in columns:
            new_columns.append(PAYMENT_DATE)
        for column in new_columns:
            # Add the column: empty for every other month, 0 for the deductions of the worked ones.
            columns.append(column)
            csv_file.lines[0] += ";" + column
            for i, other in zip(csv_file.row_lines, work.months):
                csv_file.lines[i] += ";" + ("0.00" if column != PAYMENT_DATE and not other.is_gap else "")
        new_row = {col: old_row.get(col, "") for col in columns}
        new_row.update({
            MONTH: entry.month,
            COMPANY: entry.company or "",
            POSITION: entry.position or "",
            GROSS: "" if entry.gross is None else f"{entry.gross:.2f}",
        })
        for column in deduction_columns(columns):
            amount = entry.deductions.get(column, 0.0)
            new_row[column] = "" if entry.is_gap else f"{amount:.2f}"
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
            if any(c != PAYMENT_DATE for c in new_columns):
                self.load()  # a new deduction must be of a valid base movement
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

    def delete_investment(self, index: int, fingerprint: str | None = None) -> None:
        """Delete a row of the investments CSV by position, like delete."""
        self._delete_row("investments", index, fingerprint)

    def delete_interest(self, index: int, fingerprint: str | None = None) -> None:
        """Delete a row of the interest CSV by position, like delete."""
        self._delete_row("interest", index, fingerprint)

    def _delete_row(
        self, field: str, index: int, fingerprint: str | None, debt: bool = False, card_payment: bool = False,
        correction: bool = False,
    ) -> None:
        """
        Delete a row by position from the CSV of a Snapshot `field` ("rows",
        "investments" or "interest"), optionally checking it is the expected one.
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


def work_fingerprint(row) -> str:
    """Identifies a work CSV row's content, like row_fingerprint."""
    return "|".join(str(v) for v in row.values)


def deduction_columns(columns) -> list[str]:
    """The columns of the work CSV of the deductions of the salary: all but WORK_COLUMNS."""
    return [c for c in columns if c not in WORK_COLUMNS]


def _work_month(row, deductions: list[str]) -> WorkMonth:
    """A WorkMonth from a work CSV row (all strings, empty for missing values or 0 deductions)."""
    month = row[MONTH]
    return WorkMonth(
        month=month,
        company=row[COMPANY].strip() or None,
        position=row[POSITION].strip() or None,
        gross=_parse_amount(row[GROSS], f"gross salary of {month}") if row[GROSS].strip() else None,
        deductions={
            n: _parse_amount(row[n], f"deduction {n!r} of {month}") for n in deductions if row[n].strip()
        },
        payment_date=_parse_date(row.get(PAYMENT_DATE, ""), f"received payment date of {month}"),
    )


def _with_row(rows: DataFrame, index: int, new_row: dict) -> DataFrame:
    """These rows with the values of `new_row` in the one at this position, but its id."""
    changed = rows.copy()
    for col, value in new_row.items():
        if col in changed.columns and col != ID_COLUMN:
            changed.loc[rows.index[index], col] = value
    return changed


def _conversion_row(
    snapshot: Snapshot,
    move_date: str,
    base_movement: str,
    acc_debit: str,
    acc_credit: str,
    amount_base: str | float,
    amount_other: str | float,
    comment: str,
) -> dict:
    """A conversions CSV row, validated (see _check_conversion) but against the balances."""
    new_row = {col: "" for col in snapshot.conversions.columns} | {
        "move_date": move_date,
        "base_movement": base_movement,
        "acc_debit": acc_debit,
        "acc_credit": acc_credit,
        BASE_AMOUNT: str(amount_base).strip(),
        OTHER_AMOUNT: str(amount_other).strip(),
    }
    if acc_debit == acc_credit:
        raise ValueError("Debit and credit accounts must be different")
    if base_movement not in snapshot.conversion_base_movements():
        names = sorted(snapshot.conversion_base_movements())
        raise ValueError(
            f"Conversions must be of {' or '.join(map(repr, names)) or 'a conversion base movement'}, "
            f"not {base_movement!r}"
        )
    other, base_value, other_value = _check_conversion(snapshot, new_row)
    expected = CONVERSION_BOUGHT if other.name == acc_debit else CONVERSION_SOLD
    if base_movement != expected:
        raise ValueError(f"Moving {other.currency.name} {'into' if other.name == acc_debit else 'out of'} "
                         f"{other.name} is a {expected!r} conversion, not a {base_movement!r} one")
    if base_value == 0:
        raise ValueError("The amounts of a conversion must be positive")
    new_row[BASE_AMOUNT], new_row[OTHER_AMOUNT] = f"{base_value:,.2f}", f"{other_value:,.4f}"
    if "comment" in new_row:
        new_row["comment"] = comment.replace(";", ",").strip()
    elif comment.strip():
        raise ValueError("The conversions CSV has no 'comment' column")
    return new_row


def _exchange_args(
    snapshot: Snapshot, sold: str, bought: str, acc_from: str, acc_to: str,
    amount_sold: str | float, amount_bought: str | float,
) -> tuple[str, str, str, str | float, str | float]:
    """
    The base movement, debit and credit accounts and amounts in the base and the other
    currency of a conversion selling one currency for another (see LedgerStore.add_exchange).
    """
    base_movement = exchange_base_movement(snapshot, sold, bought)
    for name, currency, side in ((acc_from, sold, "from"), (acc_to, bought, "to")):
        account = snapshot.accounts.get(name)
        if account is None:
            raise ValueError(f"Unknown account: {name!r}")
        if account.currency.name != currency:
            raise ValueError(f"The account {side} must be in {currency}, but {name} is in {account.currency.name}")
    amounts = (amount_sold, amount_bought) if sold == snapshot.base_currency.name else (amount_bought, amount_sold)
    return base_movement, acc_to, acc_from, *amounts


def is_cross(snapshot: Snapshot, sold: str, bought: str) -> bool:
    """Whether a conversion of these currencies is a cross one: between two different other currencies."""
    others = set(snapshot.currencies) - {snapshot.base_currency.name}
    return sold in others and bought in others and sold != bought


def _cross_rows(
    snapshot: Snapshot, move_date: str, sold: str, bought: str, acc_from: str, acc_to: str,
    amount_sold: str | float, amount_bought: str | float, comment: str,
) -> list[dict]:
    """
    The two conversions CSV rows of a cross conversion (see is_cross), validated but
    against the balances: a CONVERSION_SOLD of `amount_sold` out of `acc_from` into the
    INTERMEDIARY account, and a CONVERSION_BOUGHT of `amount_bought` out of it into
    `acc_to`, both of the amount sold at the value of its currency on that date, so the
    INTERMEDIARY is left as it was.
    """
    through = INTERMEDIARY
    for name, currency, side in ((acc_from, sold, "from"), (acc_to, bought, "to")):
        account = snapshot.accounts.get(name)
        if account is None:
            raise ValueError(f"Unknown account: {name!r}")
        if account.currency.name != currency:
            raise ValueError(f"The account {side} must be in {currency}, but {name} is in {account.currency.name}")
    try:
        dt.date.fromisoformat(move_date)
    except ValueError:
        raise ValueError(f"Invalid date: {move_date!r}") from None
    value = _parse_amount(amount_sold, f"amount sold in {sold}")
    base_value = round(value * _value_on(snapshot.currencies[sold], move_date, f"to convert {sold} to {bought}"), 2)
    return [
        _conversion_row(snapshot, move_date, CONVERSION_SOLD, through, acc_from, base_value, amount_sold, comment),
        _conversion_row(snapshot, move_date, CONVERSION_BOUGHT, acc_to, through, base_value, amount_bought, comment),
    ]


def conversion_pairs(snapshot: Snapshot) -> dict[int, int]:
    """
    The cross conversions of the conversions CSV (see _cross_rows): the row of each of
    their two rows to that of the other. A CONVERSION_SOLD row debiting the INTERMEDIARY
    right followed by a CONVERSION_BOUGHT one crediting it, on the same date and of the
    same amount in the base currency, of two other currencies.
    """
    rows, pairs = snapshot.conversions, {}
    for i in range(len(rows) - 1):
        sold, bought = rows.iloc[i], rows.iloc[i + 1]
        if (
            i not in pairs and sold["base_movement"] == CONVERSION_SOLD and bought["base_movement"] == CONVERSION_BOUGHT
            and sold["move_date"] == bought["move_date"] and sold["acc_debit"] == bought["acc_credit"] == INTERMEDIARY
            and _amount_of(sold[BASE_AMOUNT]) == _amount_of(bought[BASE_AMOUNT])
            and is_cross(
                snapshot, snapshot.accounts[sold["acc_credit"]].currency.name,
                snapshot.accounts[bought["acc_debit"]].currency.name,
            )
        ):
            pairs[i], pairs[i + 1] = i + 1, i
    return pairs


def _amount_of(text: str) -> float:
    """An amount of a CSV row already validated, like 1,234.50."""
    return round(float(str(text).replace(",", "").strip() or 0), 2)


def _debt_comment(snapshot: Snapshot, acc_debit: str, acc_credit: str, who: str, comment: str) -> str:
    """
    The comment of a debt movement about `who` (see LedgerStore.add_debt_movement), naming
    them first.
    """
    accounts = [snapshot.accounts.get(n) for n in (acc_debit, acc_credit)]
    debts = [a for a in accounts if isinstance(a, DebtAccount)]
    who, comment = who.strip(), comment.replace(";", ",").strip()
    if not who:
        raise ValueError("Pick who the debt movement is about")
    if debts and not any(who in a.creditors for a in debts):
        raise ValueError(f"{who!r} is not someone of the debt account {debts[0].name}")
    if any(a.creditors[who].kind == CreditorKind.COMPANY for a in debts if who in a.creditors):
        raise ValueError(f"{who!r} is a company: its debt comes from the salaries of the Work tab")
    if not debts or debts[0].creditor_in(comment) != who:
        comment = f"{who}: {comment}" if comment else who
    return comment


def _movement_row(
    snapshot: Snapshot,
    move_date: str,
    base_movement: str,
    acc_debit: str,
    acc_credit: str,
    amount: str | float,
    comment: str,
    invest: bool,
    investment: str,
    quantity: str | float,
    debt: bool,
    card_payment: bool,
    correction: bool = False,
) -> tuple[str, dict]:
    """
    Validate a movement (see LedgerStore.add) and give the Snapshot field of the CSV
    it goes to ("rows", "interest" or "investments") with its row there.
    """
    try:
        dt.date.fromisoformat(move_date)
    except ValueError:
        raise ValueError(f"Invalid date: {move_date!r}") from None
    if base_movement not in snapshot.base_movements:
        raise ValueError(f"Unknown base movement: {base_movement!r}")
    _check_invest(snapshot, base_movement, invest, "added")
    _check_tab(snapshot, base_movement, invest, debt, card_payment, correction=correction)
    has_details = str(investment).strip() or str(quantity).strip()
    if has_details and (not invest or base_movement in INTEREST):
        raise ValueError("Only movements of the investments have an investment and a quantity")
    for name in (acc_debit, acc_credit):
        if name not in snapshot.accounts:
            raise ValueError(f"Unknown account: {name!r}")
    value = _parse_amount(
        amount, "amount",
        "The amount cannot be negative; pick the opposite base movement or swap the accounts",
    )
    if acc_debit == acc_credit:
        raise ValueError("Debit and credit accounts must be different")

    base = snapshot.base_movements[base_movement]
    try:
        Movement(
            move_date=move_date,
            move_base=base,
            input_acc_debit=snapshot.accounts[acc_debit],
            input_acc_credit=snapshot.accounts[acc_credit],
            amount=value,
        )
    except AssertionError as e:
        raise ValueError(str(e)) from None
    if invest:
        _check_free_sides(base, snapshot.accounts[acc_debit], snapshot.accounts[acc_credit])

    comment = comment.replace(";", ",").strip()
    new_row = {
        "move_date": move_date,
        "base_movement": base_movement,
        "acc_debit": acc_debit,
        "acc_credit": acc_credit,
        "amount": f"{value:,.2f}",
        "comment": comment,
    }
    if not invest:
        field = "rows"
    elif base_movement in INTEREST:
        field = "interest"
        gain = base.move_type == MovementType.GAIN
        new_row = {
            "move_date": move_date,
            INTEREST_ACCOUNT: acc_debit if gain else acc_credit,
            "amount": f"{'' if gain else '-'}{value:,.2f}",
            "comment": comment,
        }
    else:
        field = "investments"
        name, units = _investment_details(base, investment, quantity)
        new_row |= {INVESTMENT_COLUMN: name, QUANTITY_COLUMN: _quantity_text(units) if units else ""}
    return field, new_row


def _check_invest(snapshot: Snapshot, base_name: str, invest: bool, action: str) -> None:
    """
    Movements of the investment base movements are only added and deleted from the
    Investments tab (`invest`), those of other internal ones never by hand but the
    ones of the Debt and Credit Cards tabs (see _check_tab), and the GENERATED ones
    are never stored.
    """
    is_invest = base_name in snapshot.invest_base_movements()
    if invest and not is_invest:
        raise ValueError(f"Base movement {base_name!r} is not an investment base movement")
    if is_invest and not invest:
        raise ValueError(f"Movements of the base movement {base_name!r} are {action} from the Investments tab")
    tab_bases = (
        snapshot.debt_base_movements().keys() | snapshot.card_payment_base_movements().keys()
        | snapshot.correction_base_movements().keys() | snapshot.conversion_base_movements().keys()
    )
    if snapshot.base_movements[base_name].internal and not is_invest and base_name not in tab_bases:
        raise ValueError(f"Base movement {base_name!r} is internal and its movements cannot be {action} by hand")
    if base_name in GENERATED:
        raise ValueError(f"The movements of the base movement {base_name!r} are generated by pledger")


def _check_known_investment(snapshot: Snapshot, acc_debit: str, acc_credit: str, name: str) -> None:
    """
    A movement of an investment account must name one of its investments: in the
    investments YAML or only in the ledger. A new one is added first (see
    LedgerStore.add_investment), with its type.
    """
    account = next((snapshot.accounts[n] for n in (acc_debit, acc_credit)
                    if isinstance(snapshot.accounts[n], InvestmentAccount)), None)
    if account is None or not name:
        return
    held = next(a for a in snapshot.investment_accounts() if a.name == account.name).investments
    if name not in account.investments and name not in held:
        raise ValueError(f"{name!r} is not an investment of {account.name}: add it as a new investment first")


def _check_tab(
    snapshot: Snapshot, base_name: str, invest: bool, debt: bool, card_payment: bool, action: str = "added",
    correction: bool = False,
) -> None:
    """
    Each tab adds (or edits, or deletes: the `action`) the movements of its own base
    movements: the conversions are only added from the Currencies tab, the debt
    movements from the Debt tab, the card payments from the Credit Cards tab, the
    balance corrections from the Accounts tab (`correction`; the Ledger tab deletes
    them with it too), and all the others from the Ledger tab (or the Investments tab,
    see _check_invest).
    """
    if base_name in snapshot.conversion_base_names:
        raise ValueError(f"The conversions ({base_name!r}) are added from the Currencies tab")
    for wanted, bases, what, tab in (
        (debt, snapshot.debt_base_movements(), "debt", "Debt"),
        (card_payment, snapshot.card_payment_base_movements(), "card payment", "Credit Cards"),
        (correction, snapshot.correction_base_movements(), "balance correction", "Accounts"),
    ):
        if wanted and base_name not in bases:
            raise ValueError(f"Base movement {base_name!r} is not a {what} base movement")
        if base_name in bases and not wanted:
            raise ValueError(f"Movements of the base movement {base_name!r} are {action} from the {tab} tab")
    if invest and (debt or card_payment):
        raise ValueError("A movement of the investments is not a debt movement or a card payment")


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


def _check_initial_base_movements(base_movements: dict[str, MovementBase]) -> None:
    """INITIAL_GAIN and INITIAL_LOSS record the initial amount of the accounts (see _required_base)."""
    _required_base(base_movements, INITIAL_GAIN, gain=True)
    _required_base(base_movements, INITIAL_LOSS, gain=False)


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


def _initial_rows(snapshot: Snapshot, first_date: str) -> DataFrame:
    """
    The movement recording the initial amount of each account, in definition order,
    as CSV rows: on its initial date, else the first date of the ledger. Increases are
    INITIAL_GAIN and decreases INITIAL_LOSS movements against the equity account. The
    initial amount of an account in another currency is in that currency (see
    own_initial_amount): its movement is at the value of the currency on that date.
    """
    base_movements, columns = snapshot.base_movements, snapshot.rows.columns
    new_rows = []
    for acc in snapshot.accounts.values():
        amount, comment = acc.initial_amount, ""
        if acc.acc_type == AccountType.EQUITY or math.isclose(amount, 0.0, abs_tol=1e-9):
            continue
        move_date = acc.initial_date.isoformat() if acc.initial_date else first_date
        if own_initial_amount(acc):
            value = _value_on(acc.currency, move_date, f"to set the initial amount of {acc.name!r}")
            comment = f"{amount:,.4f} {acc.currency.name} at {value:,.4f}"
            amount = round(amount * value, 2)
        # A positive liability is a decrease of equity, like a negative asset.
        if (amount > 0) == (acc.acc_type == AccountType.ASSET):
            base = base_movements[INITIAL_GAIN]
            acc_debit, acc_credit = acc.name, base.acc_credit.name
        else:
            base = base_movements[INITIAL_LOSS]
            acc_debit, acc_credit = base.acc_debit.name, acc.name
        new_rows.append({
            "move_date": move_date,
            "base_movement": base.name,
            "acc_debit": acc_debit,
            "acc_credit": acc_credit,
            "amount": repr(abs(amount)),
            ID_COLUMN: f"{INITIAL_ID}-{acc.name}",
            **({"comment": comment} if comment and "comment" in columns else {}),
        })
    return _generated_rows(new_rows, columns, -1)


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


def _investment_rows(snapshot: Snapshot, start: int) -> DataFrame:
    """
    The movements of the investments CSV, as CSV rows indexed from `start` down: of
    the investment base movements but the INTEREST ones. Their investments and
    quantities are checked against the investment accounts by _apply_investments.
    """
    bases = snapshot.invest_base_movements()
    new_rows = []
    for _, row in snapshot.investments.iterrows():
        if row["base_movement"] not in bases or row["base_movement"] in INTEREST:
            raise ValueError(
                "The investments CSV can only have movements of the investment base movements but the "
                f"interest on money, not of {row['base_movement']!r} ({row['move_date']})"
            )
        new_rows.append({col: row[col] for col in snapshot.rows.columns if col in row.index})
    return _generated_rows(new_rows, snapshot.rows.columns, start)


def _interest_rows(snapshot: Snapshot, start: int) -> DataFrame:
    """
    The movements of the interest CSV, as CSV rows indexed from `start` down: an
    INTEREST_GAIN debiting its account if the amount is positive, else an
    INTEREST_LOSS crediting it, against the equity account. Never of an investment
    account: the results of an investment are in the investments CSV.
    """
    new_rows = []
    for _, row in snapshot.interest.iterrows():
        date, name, text = row["move_date"], row[INTEREST_ACCOUNT], row["amount"].strip()
        account = snapshot.accounts.get(name)
        if account is None:
            raise ValueError(f"Unknown account of the interest on money: {name!r} ({date})")
        if isinstance(account, InvestmentAccount):
            raise ValueError(f"The interest on money cannot be of an investment account: {name} ({date})")  # noqa: TRY004 (a bad data value)
        gain = not text.startswith("-")
        base = _required_base(
            snapshot.base_movements, INTEREST_GAIN if gain else INTEREST_LOSS, gain, "for the interest on money"
        )
        amount = _parse_amount(text.removeprefix("-"), f"amount of the interest on money ({date})")
        new_rows.append({
            "move_date": date,
            "base_movement": base.name,
            "acc_debit": name if gain else base.acc_debit.name,
            "acc_credit": base.acc_credit.name if gain else name,
            "amount": f"{amount:,.2f}",
            **({"comment": row.get("comment", "")} if "comment" in snapshot.rows.columns else {}),
        })
    return _generated_rows(new_rows, snapshot.rows.columns, start)


def _salary_rows(snapshot: Snapshot, start: int) -> DataFrame:
    """
    The salaries of the work history, as CSV rows indexed from `start` down: the gross
    salary of each month split equally between its weeks (see weeks.Week), as a SALARY
    movement on the Monday of each one, from the first date of the ledger on, each
    followed by the deductions of that week, split the same way, crediting the account
    the salary went to (see _deduction_bases). Their comment names the company first,
    so that a debt account on their side owes them to it (see DebtAccount.creditor_in).
    """
    salary = snapshot.base_movements.get(SALARY)
    bases = _deduction_bases(snapshot, salary)
    new_rows = []
    for month in snapshot.work.months:
        if month.is_gap:
            continue
        weeks = prorate_by_week(month.month, month.gross)
        deductions = {
            n: [amount for _, amount in prorate_by_week(month.month, d)] for n, d in month.deductions.items()
        }
        for i, (week, amount) in enumerate(weeks, 1):
            if week.start.isoformat() < snapshot.start_date or (
                snapshot.previous_month and week.month <= snapshot.previous_month
            ):
                continue
            job = f"{month.company} ({month.position})"
            moves = [(SALARY, amount, "", f"{SALARY_ID}-{month.month}-{i}")] + [
                (n, d[i - 1], f"{n}, ", f"{SALARY_ID}-{month.month}-{i}-{deduction_id(n)}")
                for n, d in deductions.items()
            ]
            for name, value, what, move_id in moves:
                if value == 0:
                    continue
                new_rows.append({
                    "move_date": week.start.isoformat(),
                    "base_movement": name,
                    "amount": f"{value:,.2f}",
                    ID_COLUMN: move_id,
                    **({"comment": f"{job}: {what}week {i}/{len(weeks)} of {month.month}"}
                       if "comment" in snapshot.rows.columns else {}),
                })
    if new_rows:
        if salary is None or salary.move_type != MovementType.GAIN or salary.acc_debit is None:
            raise ValueError(
                f"The base movement {SALARY!r} is required for the salaries of the work history: "
                "a gain with a fixed debit account"
            )
        for row in new_rows:
            base = bases.get(row["base_movement"], salary)
            credit = base.acc_credit or salary.acc_debit
            row |= {"acc_debit": base.acc_debit.name, "acc_credit": credit.name}
    return _generated_rows(new_rows, snapshot.rows.columns, start)


def deduction_id(name: str) -> str:
    """The part of the ids of the movements of a deduction of the salary naming it, unchanging."""
    return hashlib.sha1(name.encode()).hexdigest()[:6]


def _deduction_bases(snapshot: Snapshot, salary: MovementBase | None) -> dict[str, MovementBase]:
    """
    The base movements of the deductions of the salary, the columns of the work CSV
    but its WORK_COLUMNS: each a non-internal loss, with a fixed debit account and
    no fixed credit one but the debit one of the SALARY, which they credit.
    """
    bases = {}
    for name in snapshot.work.deductions:
        base = snapshot.base_movements.get(name)
        if base is None:
            raise ValueError(f"The column {name!r} of the work CSV is not a base movement")
        salary_account = salary.acc_debit.name if salary is not None and salary.acc_debit else None
        if (
            base.internal or base.move_type != MovementType.LOSS or base.acc_debit is None
            or base.acc_credit is not None and base.acc_credit.name != salary_account
        ):
            raise ValueError(
                f"The column {name!r} of the work CSV must be a deduction of the salary: a non-internal "
                f"loss with a fixed debit account, crediting the account of {SALARY!r}"
            )
        bases[name] = base
    return bases


def _conversion_rows(snapshot: Snapshot, start: int) -> DataFrame:
    """
    The movements of the conversions CSV, as CSV rows indexed from `start` down, with
    the amount in the base currency (see _check_conversion).
    """
    new_rows = []
    for _, row in snapshot.conversions.iterrows():
        _check_conversion(snapshot, row)
        new_rows.append({
            **{col: row[col] for col in snapshot.rows.columns if col in row.index},
            "amount": row[BASE_AMOUNT],
        })
    return _generated_rows(new_rows, snapshot.rows.columns, start)


def exchange_base_movement(snapshot: Snapshot, sold: str, bought: str) -> str:
    """
    The base movement of a conversion selling the `sold` currency for the `bought` one:
    CONVERSION_BOUGHT if the one that isn't the base currency is bought, else
    CONVERSION_SOLD. One of them must be the base currency, and the base movement a
    conversion one.
    """
    for currency in (sold, bought):
        if currency not in snapshot.currencies:
            raise ValueError(f"Unknown currency: {currency!r}")
    base = snapshot.base_currency.name
    if (sold == base) == (bought == base):
        raise ValueError(
            f"Conversions sell or buy {base} for another currency, or one other currency for another, "
            f"not {sold} for {bought}"
        )
    name = CONVERSION_BOUGHT if sold == base else CONVERSION_SOLD
    if name not in snapshot.conversion_base_movements():
        raise ValueError(f"No {name!r} base movement: add it as an internal transfer of the base movements YAML")
    return name


def _check_conversion(snapshot: Snapshot, row) -> tuple[Account, float, float]:
    """
    A conversions CSV row must be of a conversion base movement (see
    Snapshot.conversion_base_movements), between
    an account in the base currency and one in another currency, with the amount in
    each. Returns the one in the other currency, with both amounts.
    """
    date = row["move_date"]
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        raise ValueError(f"Invalid date of a conversion: {date!r}") from None
    base = snapshot.base_movements.get(row["base_movement"])
    if base is None:
        raise ValueError(f"Unknown base movement of a conversion: {row['base_movement']!r} ({date})")
    if base.name not in snapshot.conversion_base_movements():
        raise ValueError(
            f"Conversions must be of {' or '.join(map(repr, sorted(CONVERSIONS)))} (internal transfers), "
            f"not {base.name!r} ({date})"
        )
    names = (row["acc_debit"], row["acc_credit"])
    unknown = [n for n in names if n not in snapshot.accounts]
    if unknown:
        raise ValueError(f"Unknown account of a conversion: {unknown[0]!r} ({date})")
    others = [snapshot.accounts[n] for n in names if not snapshot.accounts[n].currency.is_base_currency]
    if len(others) != 1:
        raise ValueError(
            f"Conversions must be between an account in {snapshot.base_currency.name} and one in "
            f"another currency, not {names[0]} and {names[1]} ({date})"
        )
    amounts = (
        _parse_amount(row[BASE_AMOUNT], f"amount in {snapshot.base_currency.name} of a conversion ({date})"),
        _parse_amount(row[OTHER_AMOUNT], f"amount in {others[0].currency.name} of a conversion ({date})"),
    )
    if (amounts[0] == 0) != (amounts[1] == 0):
        raise ValueError(f"The amounts of a conversion must both be zero or both positive ({date})")
    return others[0], *amounts


def _result_rows(snapshot: Snapshot, start: int) -> tuple[DataFrame, dict[str, float], dict[str, list[tuple]]]:
    """
    The results of the accounts in other currencies, as CSV rows indexed from `start`
    down, their balances in their currency and their movements: (date, amount in the
    base currency, in their own or None, and the row of the conversions CSV of a
    conversion or None). Investment accounts are left out:
    their value changes with their capital gains and losses.

    The balance of each one in its currency follows its movements: the conversions
    with their amount in it, and the others (its initial amount too) at the value of
    the currency on their date (the last one up to then, else its first one). At the
    end of each date of the currencies CSV from its first movement on, it is
    revalued: the result is a RESULT_GAIN or RESULT_LOSS movement against the equity
    account, of the difference between that balance at that value of the currency
    and its balance in the ledger, to the cent. Never in the initial week, which only
    has the initial movements: those dates are revalued on the next one after it.
    """
    accounts = {
        n: a for n, a in snapshot.accounts.items()
        if not a.currency.is_base_currency and a.acc_type != AccountType.EQUITY
        and not isinstance(a, InvestmentAccount)
    }
    # Account name to its movements: (date, amount in the base currency, in its own or None,
    # row of the conversions CSV or None).
    moves: dict[str, list[tuple[str, float, float | None, int | None]]] = {n: [] for n in accounts}

    def add(date: str, debit: str, credit: str, amount: float, other: float | None, row: int | None = None) -> None:
        for name, sign in ((debit, 1), (credit, -1)):
            if name in accounts:
                sign = sign if accounts[name].acc_type == AccountType.ASSET else -sign
                moves[name].append((date, sign * amount, None if other is None else sign * other, row))

    # The initial amounts in their currency, exactly (see _initial_rows)
    for _, row in snapshot.initial_rows.iterrows():
        acc = snapshot.accounts[row[ID_COLUMN].removeprefix(f"{INITIAL_ID}-")]
        other = abs(acc.initial_amount) if own_initial_amount(acc) else None
        add(row["move_date"], row["acc_debit"], row["acc_credit"], float(row["amount"]), other)
    rows = _concat(
        [snapshot.rows, snapshot.investment_rows, snapshot.interest_rows, snapshot.salary_rows],
        snapshot.rows.columns,
    )
    for date, debit, credit, amount in zip(rows["move_date"], rows["acc_debit"], rows["acc_credit"], rows["amount"]):
        add(date, debit, credit, float(amount.replace(",", "").replace(" ", "") or 0), None)
    for i, (_, row) in enumerate(snapshot.conversions.iterrows()):
        _, amount, other = _check_conversion(snapshot, row)
        add(row["move_date"], row["acc_debit"], row["acc_credit"], amount, other, i)

    new_rows, balances = [], {}
    week = snapshot.initial_week
    last_initial = week.end.isoformat() if week is not None else ""
    for name, acc in accounts.items():
        values = acc.currency.base_currency_mapping
        account_moves = sorted(moves[name], key=lambda m: m[0])  # stable: in their order on each date
        if not account_moves or not len(values):
            balances[name] = 0.0 if not account_moves else math.nan
            continue
        book = other = 0.0  # its balance in the base currency and in its own
        pending = iter(account_moves)
        move = next(pending, None)
        for when, value in values[values.index >= Timestamp(account_moves[0][0])].items():
            day = when.date().isoformat()
            while move is not None and move[0] <= day:
                book, other = book + move[1], other + _in_currency(move, values)
                move = next(pending, None)
            if day <= last_initial:
                continue  # the initial week only has the initial movements: revalued after it
            result = round(other * value - book, 2)
            if abs(result) < 0.005:
                continue
            gain = (result > 0) == (acc.acc_type == AccountType.ASSET)
            base = _required_base(
                snapshot.base_movements, RESULT_GAIN if gain else RESULT_LOSS, gain,
                "to revalue the accounts in other currencies", FOREIGN_CURRENCY,
            )
            sides = (name, base.acc_credit.name) if base.name == RESULT_GAIN else (base.acc_debit.name, name)
            new_rows.append({
                "move_date": day, "base_movement": base.name, "acc_debit": sides[0], "acc_credit": sides[1],
                "amount": f"{abs(result):.2f}",
                ID_COLUMN: f"{RESULT_ID}-{name}-{day}",
                **({"comment": f"{other:,.4f} {acc.currency.name} at {value:,.4f}"}
                   if "comment" in snapshot.rows.columns else {}),
            })
            book += result
        while move is not None:
            other += _in_currency(move, values)
            move = next(pending, None)
        balances[name] = float(other)
    return _generated_rows(new_rows, snapshot.rows.columns, start), balances, moves


def _in_currency(move: tuple, values) -> float:
    """The amount of a movement in the currency of these values (see _result_rows)."""
    date, amount, other = move[:3]
    if other is not None:
        return other
    up_to = values[values.index <= Timestamp(date)]
    return amount / (up_to.iloc[-1] if len(up_to) else values.iloc[0])


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


def _investment_details(base: MovementBase, investment: str, quantity: str | float) -> tuple[str, float | None]:
    """
    The investment and quantity of a movement of an investment base movement. The
    investment is required with an INVESTMENT side; the quantity (positive) for
    QUANTITY_REQUIRED, and only allowed for them and QUANTITY_OPTIONAL.
    """
    name = str(investment).strip()
    if any(c in name for c in ";\r\n"):
        raise ValueError("The investment cannot contain ';' or line breaks")
    if INVESTMENT in {base.debit_special, base.credit_special} and not name:
        raise ValueError(f"Movements of {base.name!r} must name their investment")
    text = str(quantity).strip()
    if not text:
        if base.name in QUANTITY_REQUIRED:
            raise ValueError(f"Movements of {base.name!r} must have a quantity")
        return name, None
    if base.name not in QUANTITY_REQUIRED | QUANTITY_OPTIONAL:
        raise ValueError(f"Movements of {base.name!r} cannot have a quantity")
    value = _parse_amount(text, "quantity")
    if value == 0:
        raise ValueError("The quantity must be positive")
    return name, value


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


def is_capital(base: MovementBase) -> bool:
    """A gain or loss in the value of an investment: with an INVESTMENT side, not a stock split."""
    return (
        base.result_type is not None
        and INVESTMENT in {base.debit_special, base.credit_special}
        and base.name not in QUANTITY_REQUIRED
    )


def _quantity_text(value: float) -> str:
    """A quantity as written in the QUANTITY_COLUMN, e.g. 1,234.50 or 4.6209."""
    whole, _, decimals = f"{value:,.6f}".rstrip("0").partition(".")
    return f"{whole}.{decimals.ljust(2, '0')}"


def _apply_investments(snapshot: Snapshot, accounts: dict[str, Account], rows: DataFrame) -> list[dict]:
    """
    Add the movements of the investments CSV to the investment they name, in the investment account on either side: the amount and quantity to a
    debit, taken out of a credit. Investments not in the investments YAML are added
    with no type.

    Then, the gains and losses naming an investment, but stock splits, are added to
    its results (see InvestmentAccount.results): its capital gains and losses, and
    the ones with no INVESTMENT side (dividends, broker fees...), whatever their
    other account. They go to the investment account on a side, if any, else to the
    one with an investment of that name. Returns the ones naming an investment in
    no investment account.
    """
    bases = snapshot.invest_base_movements()
    rows = rows[rows.base_movement.isin(bases)]

    def column(name: str):
        return rows[name] if name in rows.columns else [""] * len(rows)

    results = []
    for date, base, debit, credit, amount, name, quantity in zip(
        rows["move_date"], rows["base_movement"], rows["acc_debit"], rows["acc_credit"], rows["amount"],
        column(INVESTMENT_COLUMN), column(QUANTITY_COLUMN),
    ):
        try:
            _check_free_sides(bases[base], accounts[debit], accounts[credit])
            name, units = _investment_details(bases[base], name, quantity)
            value = float(amount.replace(",", "").replace(" ", "") or 0)
            sides = [accounts[n] for n in (debit, credit) if isinstance(accounts[n], InvestmentAccount)]
            for acc_name, sign in ((debit, 1), (credit, -1)):
                acc = accounts[acc_name]
                if isinstance(acc, InvestmentAccount) and name:
                    acc.add_investment(Investment(name, None), sign * value, sign * (units or 0.0))
        except ValueError as e:
            raise ValueError(f"{e} ({date}, {base})") from None
        b = bases[base]
        if name and b.result_type is not None and b.name not in QUANTITY_REQUIRED:
            results.append({"date": date, "base": base, "investment": name, "amount": value, "sides": sides})

    # After every movement, so investments bought later are known too.
    investment_accounts = [a for a in accounts.values() if isinstance(a, InvestmentAccount)]
    unattributed = []
    for r in results:
        acc = next(
            (a for a in r.pop("sides") or investment_accounts if r["investment"] in a.investments), None
        )
        if acc is None:
            unattributed.append(r)
        else:
            acc.add_result(r["investment"], r["base"], r["amount"])
    return unattributed


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


def _account_fields(
    snapshot: Snapshot,
    description: str,
    initial_amount: str | float,
    currency: str,
    is_liquid: bool,
    initial_date: str,
    checks: Sequence[str],
) -> dict:
    """
    The fields of an account in the accounts YAML, validated: without a currency, it is
    in the base currency; without an initial date, or with the Monday of the initial
    week (see Snapshot.initial_week), it has none (it starts on that one). Its `checks` are AccountCheck values.
    """
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
            date = dt.date.fromisoformat(initial_date.strip())
        except ValueError:
            raise ValueError(f"Invalid initial date: {initial_date!r}") from None
        week = snapshot.initial_week
        if week is not None and date != week.start and date <= week.end:
            raise ValueError(
                f"The initial date must be after the initial week ({week.start} to {week.end}), "
                "or left empty to start on it"
            )
        if week is None or date != week.start:
            fields["initial_date"] = date
    try:
        checks = [AccountCheck(c) for c in checks]
    except ValueError:
        raise ValueError(f"Invalid checks: {list(checks)}") from None
    if len(set(checks)) != len(checks):
        raise ValueError(f"Repeated checks: {[c.value for c in checks]}")
    if checks:
        fields["checks"] = [c.value for c in checks]
    return fields


def _replace_yaml_entry(text: str, path: tuple[str, ...], update: Callable[[dict], dict]) -> str:
    """
    Return `text` with the mapping at `path`, a path of keys from the top level (e.g.
    `("asset", "Bank")`), replaced by `update` of it, as text, so the rest of the file
    is left as is. The comments within the entry are lost.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)

    def is_content(line: str) -> bool:
        return bool(line.strip()) and not line.lstrip().startswith("#")

    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip())

    def key_of(line: str):
        try:
            parsed = yaml.safe_load(line.strip())
        except yaml.YAMLError:
            return None
        return next(iter(parsed)) if isinstance(parsed, dict) and len(parsed) == 1 else None

    head, indent, lo, hi = None, 0, 0, len(lines)
    for key in path:
        children = [i for i in range(lo, hi) if is_content(lines[i])]
        if not children:
            raise ValueError(f"{key!r} not found; edit the file by hand")
        indent = indent_of(lines[children[0]])
        head = next((i for i in children if indent_of(lines[i]) == indent and key_of(lines[i]) == key), None)
        if head is None:
            raise ValueError(f"{key!r} not found; edit the file by hand")
        lo = head + 1
        hi = next((i for i in range(lo, hi) if is_content(lines[i]) and indent_of(lines[i]) <= indent), hi)

    body = [i for i in range(head, hi) if is_content(lines[i])]
    end = body[-1] + 1
    entry = yaml.safe_load("\n".join(line[indent:] for line in lines[head:end]))[path[-1]]
    if not isinstance(entry, dict):
        raise ValueError(f"{path[-1]!r} is not a mapping; edit the file by hand")  # noqa: TRY004 (a bad config value)
    step = indent_of(lines[body[1]]) - indent if len(body) > 1 else 2
    lines[head:end] = _yaml_entry(path[-1], update(entry), indent, step)
    return newline.join(lines)


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

        # An empty section (null, ~ or {}) is a header too, emptied of its value
        header = re.compile(rf"^( {{{child_indent}}}{re.escape(key)}:)\s*(?:null|~|\{{\}})?\s*(#.*)?$")
        found = next((i for i in children if header.match(lines[i])), None)
        if found is not None:
            match = header.match(lines[found])
            lines[found] = match[1] + (f" {match[2]}" if match[2] else "")
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
