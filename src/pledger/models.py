"""models.py"""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass, field
from itertools import pairwise
from math import isclose

import numpy as np
import pandas as pd
from yaml import safe_load

from pledger.code.models import assert_accounts
from pledger.enums import AccountType, InvestmentType, LedgerSide, MovementType


def _load_yaml(path: str) -> dict:
    """Load a YAML configuration file."""
    assert path.endswith(".yaml")
    with open(path, "r", encoding="utf-8") as f:
        data = safe_load(f)
    return data


# A base movement side set to one of these (reserved names) takes any investment
# or debt account instead of a fixed one. See SPECIAL_SIDES.
INVESTMENT = "INVESTMENT"
DEBT = "DEBT"


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
        if base_move_d.get(ls_value) not in {None, INVESTMENT, DEBT}
        else None
    )
    if (move_type == MovementType.TRANSFER) and (acc is not None):
        acc_type: AccountType = acc.acc_type
        if acc_type == AccountType.EQUITY:
            raise ValueError(
                "There is a BaseMovement transfer with a fixed Equity account in the settings"
            )

    return acc


def _special_side(acc: Account | None, value: str | None) -> str | None:
    """
    INVESTMENT or DEBT if a side is set to it. Ignored on the equity side of
    gains and losses, like fixed accounts.
    """
    return value if acc is None and value in {INVESTMENT, DEBT} else None


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
    currency: Currency
    initial_amount: float
    is_liquid: bool = False
    initial_date: dt.date | None = None  # None: the first date found in the ledger

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
    def load_yaml(
        cls,
        path: str,
        base_currency: Currency,
        other_currencies: dict[str, Currency] | None = None,
        investments_path: str | None = None,
        creditors_path: str | None = None,
    ) -> dict[str, Account]:
        """
        Load Accounts from YAML path. An account without a `currency` is in the
        base currency. Every account must set its `initial_amount`, except equity
        accounts, which must not (theirs is 0.0), and investment and debt accounts.

        Investment accounts are the asset accounts listed in the investments YAML
        (if given), and debt accounts the liability accounts listed in the creditors
        YAML (if given). They must not set an `initial_amount` either: theirs is the
        sum of the initial amounts of their investments or creditors.
        """
        data = _load_yaml(path)
        investments_data = (_load_yaml(investments_path) if investments_path else None) or {}
        creditors_data = (_load_yaml(creditors_path) if creditors_path else None) or {}
        both = sorted(set(investments_data) & set(creditors_data))
        if both:
            raise ValueError(f"Accounts cannot be both investment and debt accounts: {both}")
        currencies = {base_currency.name: base_currency, **(other_currencies or {})}

        def currency_of(name: str, acc_d: dict) -> Currency:
            curr = acc_d.get("currency") or base_currency.name
            if curr not in currencies:
                raise ValueError(f"Account {name!r} has an unknown currency: {curr!r}")
            return currencies[curr]

        def is_liquid_of(name: str, acc_d: dict) -> bool:
            value = acc_d.get("is_liquid", False)
            if not isinstance(value, bool):
                raise ValueError(f"Account {name!r} is_liquid must be true or false: {value!r}")  # noqa: TRY004 (a bad config value)
            return value

        def initial_amount_of(name: str, acc_type: AccountType, acc_d: dict) -> float:
            if name in investments_data:
                if acc_type != AccountType.ASSET:
                    raise ValueError(f"Investment account {name!r} must be an asset account")
                if "initial_amount" in acc_d:
                    raise ValueError(f"Investment account {name!r} must not have an initial_amount")
                return 0.0  # set from its investments
            if name in creditors_data:
                if acc_type != AccountType.LIABILITY:
                    raise ValueError(f"Debt account {name!r} must be a liability account")
                if "initial_amount" in acc_d:
                    raise ValueError(f"Debt account {name!r} must not have an initial_amount")
                return 0.0  # set from its creditors
            if acc_type == AccountType.EQUITY:
                if "initial_amount" in acc_d:
                    raise ValueError(f"Equity account {name!r} must not have an initial_amount")
                return 0.0
            if "initial_amount" not in acc_d:
                raise ValueError(f"Account {name!r} must have an initial_amount")
            value = acc_d["initial_amount"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Account {name!r} initial_amount must be a number: {value!r}")  # noqa: TRY004 (a bad config value)
            return float(value)

        def initial_date_of(name: str, acc_d: dict) -> dt.date | None:
            value = acc_d.get("initial_date")
            if value is None or isinstance(value, dt.date):
                return value
            try:
                return dt.date.fromisoformat(str(value))
            except ValueError:
                raise ValueError(f"Account {name!r} has an invalid initial date: {value!r}") from None

        accounts = {}
        for acc_type, at_accounts in data.items():
            acc_type = AccountType(acc_type)
            for name, acc_d in at_accounts.items():
                if name in {INVESTMENT, DEBT}:
                    raise ValueError(f"{name!r} is reserved and cannot be an account name")
                fields = {
                    "name": name,
                    "description": acc_d["description"],
                    "acc_type": acc_type,
                    "currency": currency_of(name, acc_d),
                    "initial_amount": initial_amount_of(name, acc_type, acc_d),
                    "is_liquid": is_liquid_of(name, acc_d),
                    "initial_date": initial_date_of(name, acc_d),
                }
                if name in investments_data:
                    investments = InvestmentAccount.investments_from_dict(
                        name, investments_data[name] or {}
                    )
                    fields["initial_amount"] = sum(i["total_amount"] for i in investments.values())
                    accounts[name] = InvestmentAccount(**fields, investments=investments)
                elif name in creditors_data:
                    creditors = DebtAccount.creditors_from_dict(name, creditors_data[name] or {})
                    fields["initial_amount"] = sum(c.amount for c in creditors.values())
                    accounts[name] = DebtAccount(**fields, creditors=creditors)
                else:
                    accounts[name] = Account(**fields)

        for what, special_data in (("investments", investments_data), ("creditors", creditors_data)):
            unknown = sorted(set(special_data) - set(accounts))
            if unknown:
                raise ValueError(f"The {what} YAML has accounts not in the accounts YAML: {unknown}")
        return accounts


@dataclass
class Investment:
    """An investment (a bond, a stock, a fund...) held in an investment account."""

    name: str
    type: InvestmentType

    def __post_init__(self):
        self.type = InvestmentType(self.type)


@dataclass
class InvestmentAccount(Account):
    """
    Asset account that holds investments: a dict of investment name to its
    `investment` (Investment), `total_amount` ($) and `total_quantity` (float).
    A `total_quantity` never drops below 0.0.
    """

    investments: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self):
        super().__post_init__()
        for name, holding in self.investments.items():
            holding["total_quantity"] = self._checked_quantity(name, holding["total_quantity"])

    def _checked_quantity(self, name: str, quantity: float) -> float:
        if quantity < -1e-9:
            raise ValueError(
                f"The quantity of {name!r} in the account '{self.name}' cannot be negative: {quantity}"
            )
        return max(float(quantity), 0.0)

    def add_investment(self, investment: Investment, amount: float, quantity: float) -> None:
        """
        Adds an amount ($) and a quantity (negative to take out) to an investment,
        which is added to the account if new.
        """
        holding = self.investments.get(investment.name)
        if holding is None:
            holding = {"investment": investment, "total_amount": 0.0, "total_quantity": 0.0}
        elif holding["investment"].type != investment.type:
            raise ValueError(
                f"Investment {investment.name!r} in the account '{self.name}' is a "
                f"{holding['investment'].type.value}, not a {investment.type.value}"
            )
        new_quantity = self._checked_quantity(
            investment.name, holding["total_quantity"] + quantity
        )
        holding["total_amount"] += amount
        holding["total_quantity"] = new_quantity
        self.investments[investment.name] = holding

    @staticmethod
    def investments_from_dict(acc_name: str, data: dict) -> dict[str, dict]:
        """
        Investments of an account from its investments YAML dict. An investment is
        either a dict or just its type (a str), with no initial amount or quantity.
        """

        def number_of(inv_name: str, inv_d: dict, key: str) -> float:
            value = inv_d.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(  # noqa: TRY004 (a bad config value)
                    f"Investment {inv_name!r} of the account {acc_name!r} {key} must be a number: {value!r}"
                )
            return float(value)

        investments = {}
        for inv_name, inv_d in data.items():
            inv_name = str(inv_name)  # YAML keys may be numbers
            if isinstance(inv_d, str):
                inv_d = {"type": inv_d, "amount": 0.0, "quantity": 0.0}
            if not isinstance(inv_d, dict) or not inv_d.get("type"):
                raise ValueError(f"Investment {inv_name!r} of the account {acc_name!r} must have a type")
            try:
                inv_type = InvestmentType(inv_d["type"])
            except ValueError:
                raise ValueError(
                    f"Investment {inv_name!r} of the account {acc_name!r} has an unknown type: "
                    f"{inv_d['type']!r} (must be one of {[t.value for t in InvestmentType]})"
                ) from None
            investments[inv_name] = {
                "investment": Investment(name=inv_name, type=inv_type),
                "total_amount": number_of(inv_name, inv_d, "amount"),
                "total_quantity": number_of(inv_name, inv_d, "quantity"),
            }
        return investments


@dataclass
class Creditor:
    """
    Someone we have debts with in a debt account: the amount is positive if we
    owe them (a creditor) and negative if they owe us (a debtor).
    """

    name: str
    amount: float

    @property
    def is_creditor(self) -> bool:
        """We owe them."""
        return self.amount > 1e-9

    @property
    def is_debtor(self) -> bool:
        """They owe us."""
        return self.amount < -1e-9


@dataclass
class DebtAccount(Account):
    """
    Liability account that tracks debts with other people, as a dict of each
    Creditor's name to the Creditor. Its total can be negative: we are owed more
    than we owe.
    """

    creditors: dict[str, Creditor] = field(default_factory=dict)

    def __post_init__(self):
        super().__post_init__()
        self._check_positivity = lambda _: True
        self._check_positivity_vect = np.vectorize(self._check_positivity)
        self.unassigned = 0.0  # added debt whose comment names no one

    def creditor_in(self, comment: str) -> str | None:
        """
        The creditor or debtor named in a movement comment, as a whole word (ignoring
        case): the earliest one, and the longest one there, if several.
        """
        found = [
            (m.start(), -len(name), name)
            for name in self.creditors
            if (m := re.search(rf"(?<!\w){re.escape(name)}(?!\w)", comment, re.IGNORECASE))
        ]
        return min(found)[2] if found else None

    def add_debt(self, name: str | None, amount: float) -> None:
        """
        Adds an amount to what we owe someone (negative: to what they owe us), or
        to the unassigned debt without a name.
        """
        if name is None:
            self.unassigned += amount
        else:
            self.creditors[name].amount += amount

    def get_creditors(self) -> dict[str, Creditor]:
        """The ones we owe."""
        return {n: c for n, c in self.creditors.items() if c.is_creditor}

    def get_debtors(self) -> dict[str, Creditor]:
        """The ones that owe us."""
        return {n: c for n, c in self.creditors.items() if c.is_debtor}

    @staticmethod
    def creditors_from_dict(acc_name: str, data: dict) -> dict[str, Creditor]:
        """Creditors of an account from its creditors YAML dict (name: initial debt)."""
        creditors = {}
        for name, amount in data.items():
            name = str(name)  # YAML keys may be numbers
            if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                raise ValueError(  # noqa: TRY004 (a bad config value)
                    f"Creditor {name!r} of the account {acc_name!r} amount must be a number: {amount!r}"
                )
            creditors[name] = Creditor(name=name, amount=float(amount))
        return creditors


# The kind of account each special side takes, with its display name.
SPECIAL_SIDES: dict[str, type[Account]] = {INVESTMENT: InvestmentAccount, DEBT: DebtAccount}
SPECIAL_LABELS = {INVESTMENT: "Investment", DEBT: "Debt"}


def special_of(account: Account) -> str | None:
    """INVESTMENT or DEBT for investment and debt accounts, else None."""
    return next((s for s, cls in SPECIAL_SIDES.items() if isinstance(account, cls)), None)


@dataclass
class ResultType:
    """Ledger hierarchical result type."""

    id: int
    name: str
    long_name: str
    description: str
    internal: bool = False  # set by pledger itself, with ids of their own

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
    internal: bool = False  # set by pledger itself: cannot be picked or deleted by hand
    debit_special: str | None = None  # INVESTMENT or DEBT: the debit side takes any such account
    credit_special: str | None = None  # INVESTMENT or DEBT: the credit side takes any such account

    def __post_init__(self):
        self.move_type = MovementType.from_accounts(
            acc_debit=self.acc_debit,
            acc_credit=self.acc_credit,
        )
        self.debit_fixed = self.acc_debit is not None
        self.credit_fixed = self.acc_credit is not None

        for fixed, special in ((self.debit_fixed, self.debit_special), (self.credit_fixed, self.credit_special)):
            if special is not None and special not in SPECIAL_SIDES:
                raise ValueError(f"Base movement '{self.name}' has an unknown special side: {special!r}")
            if fixed and special:
                raise ValueError(
                    f"Base movement '{self.name}' cannot have both a fixed account "
                    f"and {special} on the same side"
                )

        if (self.move_type == MovementType.TRANSFER) != (self.result_type is None):
            raise ValueError(
                f"Base movement '{self.name}' must have a result type if and only if "
                f"it is a gain or a loss (it is a {self.move_type.value})"
            )
        if self.result_type is not None and self.result_type.internal != self.internal:
            raise ValueError(
                f"Base movement '{self.name}' must be internal if and only if its result type is"
            )

    def side(self, ledger_side: LedgerSide) -> str | None:
        """
        What a side takes: its fixed account name, INVESTMENT or DEBT (any
        investment or debt account) or None (any account).
        """
        if ledger_side == LedgerSide.DEBIT:
            acc, special = self.acc_debit, self.debit_special
        else:
            acc, special = self.acc_credit, self.credit_special
        return acc.name if acc is not None else special

    @classmethod
    def load_yaml(
        cls,
        path: str,
        accounts: dict[str, Account],
    ) -> tuple[dict[str, MovementBase], dict[int, ResultType]]:
        """
        Load MovementBases and ResultTypes from YAML path. A base movement is either
        a dict or just its description (a str), with no fixed accounts.

        The top-level `internal` key has the same structure (result types next to a
        `transfer` dict): those base movements and result types are internal, reserved
        for pledger itself. Result type ids are shared by internal and other ones.
        """
        data = _load_yaml(path)
        internal = dict(data.get("internal") or {})

        def result_types_of(rt_dicts: dict, is_internal: bool) -> dict[int, ResultType]:
            return {
                rt_dict["id"]: ResultType(
                    id=rt_dict["id"],
                    name=name,
                    long_name=rt_dict["long_name"],
                    description=rt_dict["description"],
                    internal=is_internal,
                )
                for name, rt_dict in rt_dicts.items()
            }

        def sections_of(rt_dicts: dict, result_types: dict[int, ResultType]) -> list[tuple]:
            """Gains and losses, grouped by ResultType."""
            sections = [
                (result_types[rt["id"]], move_type, mt_base_moves)
                for rt in rt_dicts.values()
                for move_type, mt_base_moves in (rt.get("base_movements") or {}).items()
            ]
            if any(
                move_type not in {MovementType.GAIN.value, MovementType.LOSS.value}
                for _, move_type, _ in sections
            ):
                raise ValueError(
                    "Result types base_movements must only include 'gain' and 'loss' dicts"
                )
            return sections

        internal_transfers = internal.pop(MovementType.TRANSFER.value, None)
        repeated = sorted(set(data["result_types"]) & set(internal))
        if repeated:
            raise ValueError(f"Result type names are both internal and not: {repeated}")
        ids = sorted(rt["id"] for rt in [*data["result_types"].values(), *internal.values()])
        if ids != list(range(1, len(ids) + 1)):
            raise ValueError(
                "ResultType ids (internal ones included) must be unique and go from 1 to "
                f"{len(ids)}. Values seen: {ids}"
            )
        result_types = {
            **result_types_of(data["result_types"], is_internal=False),
            **result_types_of(internal, is_internal=True),
        }

        sections = [
            *((rt, mt, bms, False) for rt, mt, bms in sections_of(data["result_types"], result_types)),
            (None, MovementType.TRANSFER.value, data.get("transfer"), False),
            *((rt, mt, bms, True) for rt, mt, bms in sections_of(internal, result_types)),
            (None, MovementType.TRANSFER.value, internal_transfers, True),
        ]

        move_bases = {}
        for result_type, move_type, mt_base_moves, is_internal in sections:
            move_type = MovementType(move_type)
            for name, base_move_d in (mt_base_moves or {}).items():
                if name in move_bases:
                    raise ValueError(f"Duplicated base movement name: {name!r}")
                if isinstance(base_move_d, str):
                    base_move_d = {"description": base_move_d}
                if "internal" in base_move_d:
                    raise ValueError(
                        f"Base movement {name!r} must not set 'internal': "
                        "list it under the top-level 'internal' key instead"
                    )
                acc_debit = _coalesce_mt_account(accounts, move_type, base_move_d, LedgerSide.DEBIT)
                acc_credit = _coalesce_mt_account(accounts, move_type, base_move_d, LedgerSide.CREDIT)
                move_bases[name] = MovementBase(
                    name=name,
                    description=base_move_d["description"],
                    acc_debit=acc_debit,
                    acc_credit=acc_credit,
                    result_type=result_type,
                    internal=is_internal,
                    debit_special=_special_side(acc_debit, base_move_d.get("debit")),
                    credit_special=_special_side(acc_credit, base_move_d.get("credit")),
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

        for ledger_side, input_acc, special in (
            (LedgerSide.DEBIT, self.input_acc_debit, self.move_base.debit_special),
            (LedgerSide.CREDIT, self.input_acc_credit, self.move_base.credit_special),
        ):
            if not special:
                continue
            kind = SPECIAL_LABELS[special].lower()
            assert isinstance(input_acc, SPECIAL_SIDES[special]), (
                f"{ledger_side.value.capitalize()} account must be "
                f"{'an' if kind[0] in 'aeiou' else 'a'} {kind} account: {input_acc.name} is not ({self.move_date})"
            )

        self.acc_debit = self.input_acc_debit
        self.acc_credit = self.input_acc_credit


# * ---------------------------------------


def parse_month(value, what: str) -> str:
    """A month as YYYY-MM, from that text or a date (whose month is taken)."""
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m")
    value = str(value).strip()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError(f"Invalid {what}: {value!r} (must be YYYY-MM)")
    return value


def next_month(month: str) -> str:
    """The month after a YYYY-MM month."""
    year, mon = int(month[:4]), int(month[5:])
    return f"{year + mon // 12:04d}-{mon % 12 + 1:02d}"


@dataclass
class WorkMonth:
    """
    A month (YYYY-MM) of the work history: the company and job position worked at,
    with the salary, gross and net, and when it was received, if it was. A month
    without a company is a gap (no job) and has no salary.
    """

    month: str
    company: str | None = None
    position: str | None = None
    gross: float | None = None
    net: float | None = None
    payment_date: dt.date | None = None

    def __post_init__(self):
        self.month = parse_month(self.month, "month")
        if (self.company is None) != (self.position is None):
            raise ValueError(f"{self.month} must have both a company and a job position, or neither")
        if self.is_gap:
            if self.gross is not None or self.net is not None or self.payment_date is not None:
                raise ValueError(f"{self.month} has no job, so it cannot have a salary")
            return
        if self.gross is None or self.net is None:
            raise ValueError(f"{self.month} must have both a gross and a net salary")
        if self.gross < 0 or self.net < 0:
            raise ValueError(f"The salary of {self.month} cannot be negative")
        if self.net > self.gross + 1e-9:
            raise ValueError(f"The net salary of {self.month} cannot be over the gross salary")

    @property
    def is_gap(self) -> bool:
        return self.company is None

    @staticmethod
    def check_sequence(months: list[WorkMonth]) -> None:
        """One per month, in order and with no missing months: gaps must be explicit."""
        for prev, cur in pairwise(months):
            if cur.month != next_month(prev.month):
                raise ValueError(
                    f"{cur.month} comes after {prev.month}, but {next_month(prev.month)} is expected: "
                    "months must be in order, one per row, with gaps as rows with no company"
                )


@dataclass
class JobPosition:
    """A job position at a company, with the months worked in it (in order)."""

    name: str
    company: str
    months: list[str] = field(default_factory=list)

    @property
    def start(self) -> str:
        return self.months[0]

    @property
    def end(self) -> str:
        return self.months[-1]


@dataclass
class Company:
    """A company worked for, with its job positions (the newest first)."""

    name: str
    positions: dict[str, JobPosition] = field(default_factory=dict)

    @property
    def start(self) -> str:
        return min(p.start for p in self.positions.values())

    @property
    def end(self) -> str:
        return max(p.end for p in self.positions.values())

    @property
    def months(self) -> int:
        return sum(len(p.months) for p in self.positions.values())

    @classmethod
    def from_months(cls, months: list[WorkMonth]) -> dict[str, Company]:
        """The companies and job positions of the work months, from the newest to the oldest."""
        companies: dict[str, Company] = {}
        for m in months:
            if m.is_gap:
                continue
            company = companies.setdefault(m.company, cls(m.company))
            company.positions.setdefault(m.position, JobPosition(m.position, m.company)).months.append(m.month)
        for c in companies.values():
            c.positions = dict(sorted(c.positions.items(), key=lambda kv: kv[1].end, reverse=True))
        return dict(sorted(companies.items(), key=lambda kv: kv[1].end, reverse=True))
