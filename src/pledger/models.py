"""models.py"""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass
from math import isclose

import numpy as np
import pandas as pd
from yaml import safe_load

from pledger.code.models import assert_accounts
from pledger.enums import AccountType, LedgerSide, MovementType


def _load_yaml(path: str) -> dict:
    """Load a YAML configuration file."""
    assert path.endswith(".yaml")
    with open(path, "r", encoding="utf-8") as f:
        data = safe_load(f)
    return data


def _coalesce_mt_account(
    accounts: dict,
    move_type: MovementType,
    base_move_d: dict,
    ledger_side: LedgerSide,
):
    """Chooses the correct Account based on the settings given."""
    ls_value = ledger_side.value
    equity_side = (
        MovementType.LOSS
        if ls_value == LedgerSide.DEBIT.value
        else MovementType.GAIN
    )

    acc = (
        accounts["Eq"]
        if move_type == equity_side
        else accounts[base_move_d[ls_value]]
        if ls_value in base_move_d
        else None
    )
    if (move_type == MovementType.TRANSFER) and (acc is not None):
        acc_type: AccountType = acc.acc_type
        if acc_type == AccountType.EQUITY:
            raise ValueError(
                "There is a BaseMovement transfer with a fixed Equity account in the settings"
            )

    return acc


# * ---------------------------------------


@dataclass
class Currency:
    """Ledger currency."""

    name: str
    description: str
    base_currency_mapping: pd.Series | None

    def __post_init__(self):
        self.is_base_currency = self.base_currency_mapping is None

    @classmethod
    def load_yaml(cls, path: str) -> tuple[Currency, dict[str, Currency]]:
        """
        Load Currencies from YAML path.

        Args:
            path (str): Path to currencies.yaml

        Returns:
            Currency: Base currency for all general information layout.
            dict[str, Currency]: Other mapped currencies (if any), whose value
                mappings are read from `currencies/<NAME>.csv` next to the YAML.
        """
        data = _load_yaml(path)
        mappings_dir = os.path.join(os.path.dirname(path), "currencies")

        base_currency = cls(
            name=data["base_currency"]["name"],
            description=data["base_currency"]["description"],
            base_currency_mapping=None,
        )

        currencies = data.get("other_currencies") or []
        assert all(
            re.match(r"^[A-Z]{1,10}$", curr["name"])
            for curr in [data["base_currency"], *currencies]
        ), "Currencies names must be A-Z from 1 to 10 characters each"

        other_currencies = {
            curr["name"]: cls(
                name=curr["name"],
                description=curr["description"],
                base_currency_mapping=(
                    pd.read_csv(
                        os.path.join(mappings_dir, f"{curr['name']}.csv"),
                        usecols=["date", "unit_value_in_base_currency"],
                        sep=";",
                        parse_dates=["date"],
                    )
                    .set_index("date", drop=True)
                    .unit_value_in_base_currency
                    .sort_index()
                ),
            )
            for curr in currencies
        }
        if base_currency.name in other_currencies:
            raise ValueError(f"Currency {base_currency.name!r} is both the base and another currency")

        return base_currency, other_currencies


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
        data = _load_yaml(path)
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
class ResultType:
    """Ledger hierarchical result type."""

    id: int
    name: str
    long_name: str
    description: str

    def __post_init__(self):
        if self.id <= 0:
            raise ValueError(f"The ResultType id must be 1-indexed. Value seen: {self.id}")


@dataclass
class MovementBase:
    """Ledger money movement base."""

    name: str
    description: str
    acc_debit: Account | None
    acc_credit: Account | None
    result_type: ResultType | None

    def __post_init__(self):
        self.move_type = MovementType.from_accounts(
            acc_debit=self.acc_debit,
            acc_credit=self.acc_credit,
        )
        self.debit_fixed = self.acc_debit is not None
        self.credit_fixed = self.acc_credit is not None

        if (self.move_type == MovementType.TRANSFER) != (self.result_type is None):
            raise ValueError(
                f"Base movement '{self.name}' must have a result type if and only if "
                f"it is a gain or a loss (it is a {self.move_type.value})"
            )

    @classmethod
    def load_yaml(
        cls,
        path: str,
        accounts: dict[str, Account],
    ) -> tuple[dict[str, MovementBase], dict[int, ResultType]]:
        """Load MovementBases and ResultTypes from YAML path."""
        data = _load_yaml(path)

        # Create ResultTypes
        result_types = {
            rt_dict["id"]: ResultType(
                id=rt_dict["id"],
                name=name,
                long_name=rt_dict["long_name"],
                description=rt_dict["description"],
            )
            for name, rt_dict in data["result_types"].items()
        }
        ids = sorted(rt["id"] for rt in data["result_types"].values())
        if ids != list(range(1, len(ids) + 1)):
            raise ValueError(
                f"ResultType ids must be unique and go from 1 to {len(ids)}. Values seen: {ids}"
            )

        # Gains and losses, grouped by ResultType; then transfers
        sections = [
            (result_types[rt["id"]], move_type, mt_base_moves)
            for rt in data["result_types"].values()
            for move_type, mt_base_moves in (rt["base_movements"] or {}).items()
        ]
        if any(
            move_type not in {MovementType.GAIN.value, MovementType.LOSS.value}
            for _, move_type, _ in sections
        ):
            raise ValueError(
                "Result types base_movements must only include 'gain' and 'loss' dicts"
            )
        sections.append((None, MovementType.TRANSFER.value, data.get("transfer")))

        move_bases = {}
        for result_type, move_type, mt_base_moves in sections:
            move_type = MovementType(move_type)
            for name, base_move_d in (mt_base_moves or {}).items():
                if name in move_bases:
                    raise ValueError(f"Duplicated base movement name: {name!r}")
                move_bases[name] = MovementBase(
                    name=name,
                    description=base_move_d["description"],
                    acc_debit=_coalesce_mt_account(
                        accounts, move_type, base_move_d, LedgerSide.DEBIT,
                    ),
                    acc_credit=_coalesce_mt_account(
                        accounts, move_type, base_move_d, LedgerSide.CREDIT,
                    ),
                    result_type=result_type,
                )

        return move_bases, result_types


@dataclass
class Movement:
    """Ledger money movement."""

    move_date: dt.date
    move_base: MovementBase
    input_acc_debit: Account
    input_acc_credit: Account
    amount: float  # ? in base currency

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
