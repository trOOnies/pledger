"""models.py"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from math import isclose

import numpy as np
from yaml import safe_load

from pledger.code.models import assert_accounts
from pledger.enums import AccountType, LedgerSide, MovementType


@dataclass
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
            else (lambda t: t >= -1e-9)
        )
        self._check_positivity_vect = np.vectorize(self._check_positivity)

    def add(self, value: float) -> None:
        """Adds a single value to the total."""
        if isclose(value, 0.0, abs_tol=1e-9):
            return

        new_total = self.total + value
        if not self._check_positivity(new_total):
            raise ValueError(
                f"The added value={value} would go against the account '{self.name}' positivity setting"
            )
        self.total = new_total

    def add_many(self, values: list[float], strict: bool = False) -> None:
        """Adds many values to the total."""
        if not values:
            return

        new_total = self.total + sum(values)
        if not self._check_positivity(new_total):
            raise ValueError(
                f"The {len(values)} added values would break the account '{self.name}' positivity setting"
            )

        if strict:
            all_subtotals = np.array(values, dtype=float).cumsum() + self.total
            if not self._check_positivity_vect(all_subtotals).all():
                raise ValueError(
                    f"The {len(values)} added values would break the account '{self.name}' strict positivity setting"
                )

        self.total = new_total

    @classmethod
    def load_yaml(cls, path: str) -> dict[str, Account]:
        """Load Accounts from YAML path."""
        assert path.endswith(".yaml")
        with open(path, "r", encoding="utf-8") as f:
            data = safe_load(f)
        return {
            name: Account(
                name=name,
                description=acc_d["description"],
                acc_type=AccountType(acc_type),
            )
            for acc_type, at_accounts in data.items()
            for name, acc_d in at_accounts.items()
        }


@dataclass
class MovementBase:
    """Ledger money movement base."""

    name: str
    description: str
    acc_debit: Account | None
    acc_credit: Account | None

    def __post_init__(self):
        self.move_type = MovementType.from_accounts(
            acc_debit=self.acc_debit,
            acc_credit=self.acc_credit,
        )
        self.debit_fixed = self.acc_debit is not None
        self.credit_fixed = self.acc_credit is not None

    @classmethod
    def load_yaml(
        cls,
        path: str,
        accounts: dict[str, Account],
    ) -> dict[str, MovementBase]:
        """Load MovementBases from YAML path."""
        assert path.endswith(".yaml")
        with open(path, "r", encoding="utf-8") as f:
            data = safe_load(f)
        return {
            name: MovementBase(
                name=name,
                description=base_move_d["description"],
                acc_debit=(
                    accounts["Eq"]
                    if move_type == MovementType.LOSS
                    else accounts[base_move_d[LedgerSide.DEBIT.value]]
                    if LedgerSide.DEBIT.value in base_move_d
                    else None
                ),
                acc_credit=(
                    accounts["Eq"]
                    if move_type == MovementType.GAIN
                    else accounts[base_move_d[LedgerSide.CREDIT.value]]
                    if LedgerSide.CREDIT.value in base_move_d
                    else None
                ),
            )
            for move_type, mt_base_moves in data.items()
            for name, base_move_d in mt_base_moves.items()
        }


@dataclass
class Movement:
    """Ledger money movement."""

    move_date: dt.date
    move_base: MovementBase
    input_acc_debit: Account
    input_acc_credit: Account
    amount: float  # ? in local currency

    def __post_init__(self):
        self.move_type = self.move_base.move_type

        c_gain = self.input_acc_credit.acc_type == AccountType.EQUITY
        c_loss = self.input_acc_debit.acc_type == AccountType.EQUITY

        if self.move_type == MovementType.GAIN:
            assert c_gain and not c_loss, "Movement type must match"
            if self.move_base.debit_fixed:
                assert_accounts(
                    LedgerSide.DEBIT,
                    self.input_acc_debit,
                    self.move_base.acc_debit,
                    self.move_date,
                )
        elif self.move_type == MovementType.LOSS:
            assert c_loss and not c_gain, "Movement type must match"
            if self.move_base.credit_fixed:
                assert_accounts(
                    LedgerSide.CREDIT,
                    self.input_acc_credit,
                    self.move_base.acc_credit,
                    self.move_date,
                )
        elif self.move_type == MovementType.TRANSFER:
            assert not c_loss and not c_gain, "Movement type must match"
            if self.move_base.debit_fixed:
                assert_accounts(
                    LedgerSide.DEBIT,
                    self.input_acc_debit,
                    self.move_base.acc_debit,
                    self.move_date,
                )
            if self.move_base.credit_fixed:
                assert_accounts(
                    LedgerSide.CREDIT,
                    self.input_acc_credit,
                    self.move_base.acc_credit,
                    self.move_date,
                )
        else:
            raise ValueError(f"Unknown movement type: {self.move_type}")

        self.acc_debit = self.input_acc_debit
        self.acc_credit = self.input_acc_credit

    def print_data(self) -> None:
        print(
            self.move_date
            + "\t"
            + self.acc_debit.name
            + "\t"
            + str(self.amount)
            + "\t"
            + self.acc_credit.name
        )
