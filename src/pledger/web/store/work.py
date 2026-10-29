"""work.py: the work history, and the salaries generated from it."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pandas import DataFrame

from pledger.enums import MovementType
from pledger.models import Company, JobPosition, MovementBase, WorkMonth
from pledger.web.store.common import _generated_rows, _parse_amount, _parse_date
from pledger.web.store.constants import (
    COMPANY,
    GROSS,
    ID_COLUMN,
    MONTH,
    PAYMENT_DATE,
    POSITION,
    SALARY,
    SALARY_ID,
    WORK_COLUMNS,
)
from pledger.weeks import prorate_by_week

if TYPE_CHECKING:
    from pledger.web.store.snapshot import Snapshot


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
