"""models.py"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from pandas import read_csv

from enums import AccountType, MovementType


@dataclass(slots=True)
class Account:
    """Ledger account."""
    name: str
    description: str
    acc_type: AccountType

    def __post_init__(self):
        self.total = 0

        self._check_positivity = (
            (lambda _: True)
            if self.acc_type == AccountType.EQUITY
            else (lambda t: not (t < 0.0))
        )

    def add(self, value: float) -> None:
        """Adds a single value to the total."""
        self.total += value
        self._check_positivity(self.total)

    def add_many(self, values: list[float]) -> None:
        """Adds many values to the total."""
        self.total += sum(values)
        self._check_positivity(self.total)


@dataclass(slots=True)
class Movement:
    """Ledger money movement."""
    move_date: dt.date
    acc_debit: Account
    acc_credit: Account
    amount: float  # ? in local currency

    def __post_init__():
        move_type: MovementType = ...  # TODO


@dataclass(slots=True)
class Ledger:
    movements: list[Movement]

    def __post_init__():
        pass

    @classmethod
    def from_csv(cls, path: str) -> Ledger:
        df = read_csv(
            path,
            names=["move_date", "acc_debit", "acc_credit", "amount"],
        )
