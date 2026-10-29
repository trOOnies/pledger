"""models.py"""

from __future__ import annotations

import datetime as dt
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import pairwise
from math import isclose

import numpy as np
import pandas as pd
from yaml import safe_load

from pledger.code.models import assert_accounts
from pledger.enums import (
    AccountCheck,
    AccountType,
    CreditorKind,
    DebtRole,
    InvestmentType,
    LedgerSide,
    MovementType,
)
from pledger.weeks import Week


def _load_yaml(path: str) -> dict:
    """Load a YAML configuration file."""
    assert path.endswith(".yaml")
    with open(path, "r", encoding="utf-8") as f:
        data = safe_load(f)
    return data


# A base movement side set to one of these (reserved names) takes any investment, credit
# card or foreign currency account instead of a fixed one. See SPECIAL_SIDES.
INVESTMENT = "INVESTMENT"
CREDIT_CARD = "CREDIT CARD"
FOREIGN_CURRENCY = "FOREIGN CURRENCY"
RESERVED = {INVESTMENT, CREDIT_CARD, FOREIGN_CURRENCY}

# The internal accounts, mandatory, under the top-level `internal` key of the accounts
# YAML: the intermediary between movements (in the base currency, e.g. of the
# conversions between two other currencies), the debts with other people (the only debt
# account, see DebtAccount) and the equity (the only equity account).
INTERMEDIARY, DEBT_ACCOUNT, EQUITY_ACCOUNT = "Fi", "De", "Eq"
INTERNAL_ACCOUNTS = {
    INTERMEDIARY: AccountType.ASSET, DEBT_ACCOUNT: AccountType.LIABILITY, EQUITY_ACCOUNT: AccountType.EQUITY,
}
INTERNAL = "internal"

INITIAL_DATE = "initial_date"  # of an account in the accounts YAML
# The top-level key of the accounts YAML with the last past month left out of the
# movements, an initial picture: its last week (see initial_week) has the initial
# movements of the accounts, and nothing else (see Account.load_yaml).
PREVIOUS_MONTH = "previous_month"

INFLATION_COLUMN = "monthly_inflation"  # of the base currency, in base_currency.csv
CPI_BASE = 100.0  # the CPI at the end of the day before the first month of base_currency.csv


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
        accounts[EQUITY_ACCOUNT]
        if move_type == equity_side
        else accounts[base_move_d[ls_value]]
        if base_move_d.get(ls_value) not in {None, *RESERVED}
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
    INVESTMENT, CREDIT_CARD or FOREIGN_CURRENCY if a side is set to it. Ignored on the
    equity side of gains and losses, like fixed accounts.
    """
    return value if acc is None and value in RESERVED else None


# * ---------------------------------------


def daily_cpi(monthly_inflation: pd.Series) -> pd.Series:
    """
    The customer price index at the end of each day, from the inflation of each
    month (ratios, indexed by consecutive monthly Periods). It is CPI_BASE at the end
    of the day before the first month, and each month's inflation compounds evenly
    over its days: on its day `d` of `n`, the CPI is the previous month's last one
    times (1 + inflation) ** (d / n), so on its last day it is grown by the whole
    month's inflation.
    """
    if not len(monthly_inflation):
        return pd.Series(dtype=float, index=pd.DatetimeIndex([]))
    growth = 1 + monthly_inflation
    month_start = CPI_BASE * growth.cumprod().shift(1, fill_value=1.0)  # the CPI before each month
    dates = pd.date_range(
        monthly_inflation.index[0].start_time, monthly_inflation.index[-1].end_time.normalize(), freq="D"
    )
    months = dates.to_period("M")
    cpi = (
        month_start.reindex(months).to_numpy()
        * growth.reindex(months).to_numpy() ** (dates.day / dates.days_in_month).to_numpy()
    )
    before = dates[0] - pd.Timedelta(days=1)
    return pd.concat([pd.Series([CPI_BASE], index=[before]), pd.Series(cpi, index=dates)])


@dataclass
class Currency:
    """Ledger currency."""

    name: str
    description: str
    base_currency_mapping: pd.Series | None  # its value in the base currency by date; None for the base one
    # The inflation of each month (a ratio, by monthly Period), only for the base currency
    monthly_inflation: pd.Series | None = None

    def __post_init__(self):
        self.is_base_currency = self.base_currency_mapping is None
        # The CPI at the end of each day, from the monthly inflation (see daily_cpi)
        self.customer_price_index = (
            None if self.monthly_inflation is None else daily_cpi(self.monthly_inflation)
        )

    @classmethod
    def load_yaml(cls, path: str) -> tuple[Currency, dict[str, Currency]]:
        """
        Load Currencies from YAML path.

        Args:
            path (str): Path to currencies.yaml

        Returns:
            Currency: Base currency for all general information layout.
            dict[str, Currency]: Other mapped currencies (if any).

        Their values are read from the CSVs next to the YAML: `base_currency.csv`
        has the `monthly_inflation` of the base currency for each `month`, and
        `currencies.csv` a `date` column and one column per other currency, with its
        value in the base currency (empty cells are dates without a value).
        """
        data = _load_yaml(path)

        currencies = data.get("other_currencies") or []
        assert all(
            re.match(r"^[A-Z]{1,10}$", curr["name"])
            for curr in [data["base_currency"], *currencies]
        ), "Currencies names must be A-Z from 1 to 10 characters each"
        names = [curr["name"] for curr in currencies]
        if data["base_currency"]["name"] in names:
            raise ValueError(
                f"Currency {data['base_currency']['name']!r} is both the base and another currency"
            )

        folder = os.path.dirname(path)
        values = cls._load_values(os.path.join(folder, "currencies.csv"), names)
        base_currency = cls(
            name=data["base_currency"]["name"],
            description=data["base_currency"]["description"],
            base_currency_mapping=None,
            monthly_inflation=cls._load_inflation(os.path.join(folder, "base_currency.csv")),
        )
        other_currencies = {
            curr["name"]: cls(
                name=curr["name"],
                description=curr["description"],
                base_currency_mapping=values[curr["name"]].dropna(),
            )
            for curr in currencies
        }
        return base_currency, other_currencies

    @staticmethod
    def _load_inflation(path: str) -> pd.Series:
        """
        The base currency CSV: the inflation of each month, a percentage (e.g. `2.90%`,
        the `%` optional), as a ratio by monthly Period. Months must be consecutive.
        """
        rows = pd.read_csv(path, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
        missing = [c for c in ("month", INFLATION_COLUMN) if c not in rows.columns]
        if missing:
            raise ValueError(f"The base currency CSV is missing the columns {missing}")
        bad = [m for m in rows.month if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", m)]
        if bad:
            raise ValueError(f"The base currency CSV has months not like YYYY-MM: {bad}")
        months = pd.PeriodIndex(rows.month, freq="M")
        gaps = [str(b) for a, b in pairwise(months) if b != a + 1]
        if gaps:
            raise ValueError(
                f"The base currency CSV months must be consecutive, in order, but {gaps} are not"
            )
        texts = rows[INFLATION_COLUMN].str.strip().str.removesuffix("%").str.strip()
        if (texts == "").any():
            raise ValueError(
                f"The base currency CSV has no inflation for {list(rows.month[texts == ''])}"
            )
        inflation = pd.Series(pd.to_numeric(texts).astype(float).to_numpy() / 100, index=months)
        if (inflation <= -1).any():
            raise ValueError("The base currency CSV inflation must be above -100%")
        return inflation

    @staticmethod
    def _load_values(path: str, names: list[str]) -> pd.DataFrame:
        """
        The currencies CSV, indexed and sorted by date, with a column per currency.
        Optional without other currencies.
        """
        if not names and not os.path.exists(path):
            return pd.DataFrame(index=pd.DatetimeIndex([], name="date"))
        values = (
            pd.read_csv(path, sep=";", parse_dates=["date"], encoding="utf-8-sig")
            .set_index("date", drop=True)
            .sort_index()
        )
        missing = [c for c in names if c not in values.columns]
        if missing:
            raise ValueError(f"The currencies CSV is missing the columns {missing}")
        unknown = [c for c in values.columns if c not in names]
        if unknown:
            raise ValueError(f"The currencies CSV has columns of unknown currencies: {unknown}")
        repeated = values.index[values.index.duplicated()]
        if len(repeated):
            raise ValueError(
                f"The currencies CSV repeats the dates {[d.date().isoformat() for d in repeated]}"
            )
        values.index = pd.to_datetime(values.index)  # not parsed without rows
        values = values.apply(pd.to_numeric).astype(float)
        if (values <= 0).any().any():
            raise ValueError("The currencies CSV values must be positive")
        return values


@dataclass
class Account:
    """Ledger account."""

    name: str
    description: str
    acc_type: AccountType
    currency: Currency
    initial_amount: float  # in its currency
    is_liquid: bool = False
    initial_date: dt.date | None = None  # None: the first date of the ledger
    checks: tuple[AccountCheck, ...] = ()  # on its daily balance in the ledger (see Ledger.check_accounts)
    internal: bool = False  # one of the INTERNAL_ACCOUNTS

    def __post_init__(self):
        self.total = 0
        self.checks = tuple(AccountCheck(c) for c in self.checks)

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
        companies: list[str] | None = None,
        credit_cards_path: str | None = None,
        initial_prices: dict[str, dict[str, float]] | None = None,
    ) -> dict[str, Account]:
        """
        Load Accounts from YAML path. An account without a `currency` is in the
        base currency. Every account must set its `initial_amount`, in its currency,
        except equity accounts, which must not (theirs is 0.0), and investment and
        debt accounts. An account without an `initial_date` starts on the initial
        week of the top-level `previous_month` of the YAML (see previous_month_of_yaml),
        if any: on its Monday. One with it must start after that week.

        The top-level `internal` key has the same sections, with exactly the
        INTERNAL_ACCOUNTS, which no other section has: the asset Fi and the liability
        De, in the base currency, and the equity Eq, the only equity account.

        Investment accounts are the asset accounts listed in the investments YAML
        (if given), and De is the debt account, with its creditors in the creditors
        YAML (if given, with no other account). They must not set an `initial_amount`
        either: theirs is the sum of the initial amounts of their investments (their
        initial quantity at their initial price, of `initial_prices`: account name to
        investment name to price) or creditors. De has the `companies` (of the work history), with no initial debt
        unless its `companies` section sets one.

        Credit card accounts are the ones in the credit cards YAML (if given), set
        like the others but always liquid liabilities.

        Any account may list `checks` (AccountCheck values) on its daily balance.
        """
        data = dict(_load_yaml(path) or {})
        previous_month = previous_month_of_yaml(data)
        week = initial_week(previous_month) if previous_month else None
        data.pop(PREVIOUS_MONTH, None)
        internal_data = data.pop(INTERNAL, None) or {}
        if not isinstance(internal_data, dict):
            raise ValueError(f"The accounts YAML {INTERNAL!r} key must be a mapping of account types")  # noqa: TRY004 (a bad config value)
        found = {name: t for t, at_accounts in internal_data.items() for name in (at_accounts or {})}
        expected = {n: t.value for n, t in INTERNAL_ACCOUNTS.items()}
        if found != expected:
            raise ValueError(
                f"The accounts YAML {INTERNAL!r} key must have exactly the asset {INTERMEDIARY}, the liability "
                f"{DEBT_ACCOUNT} and the equity {EQUITY_ACCOUNT}: it has {found or 'none'}"
            )
        for t, at_accounts in data.items():
            if AccountType(t) == AccountType.EQUITY and at_accounts:
                raise ValueError(f"The only equity account is the internal {EQUITY_ACCOUNT}: {sorted(at_accounts)}")
            internal_names = sorted(set(at_accounts or {}) & set(INTERNAL_ACCOUNTS))
            if internal_names:
                raise ValueError(f"The accounts {internal_names} are internal: under the {INTERNAL!r} key only")
        investments_data = (_load_yaml(investments_path) if investments_path else None) or {}
        creditors_data = (_load_yaml(creditors_path) if creditors_path else None) or {}
        others = sorted(set(creditors_data) - {DEBT_ACCOUNT})
        if others:
            raise ValueError(f"The creditors YAML only has the creditors of {DEBT_ACCOUNT}, not of {others}")
        both = sorted(set(investments_data) & set(INTERNAL_ACCOUNTS))
        if both:
            raise ValueError(f"The internal accounts cannot be investment accounts: {both}")
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
            if name == DEBT_ACCOUNT:
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
            value = _parse_date(acc_d.get(INITIAL_DATE), f"Account {name!r} has an invalid initial date")
            if week is None:
                return value
            if value is None or value == week.start:
                return week.start
            if value <= week.end:
                raise ValueError(
                    f"Account {name!r} cannot start ({value}) before the end of the initial week "
                    f"({week.start} to {week.end}, the last one of {previous_month}): leave its "
                    "initial_date out to start on it"
                )
            return value

        def checks_of(name: str, acc_d: dict) -> tuple[AccountCheck, ...]:
            values = acc_d.get("checks") or []
            if not isinstance(values, list):
                raise ValueError(f"Account {name!r} checks must be a list: {values!r}")  # noqa: TRY004 (a bad config value)
            valid = [c.value for c in AccountCheck]
            unknown = [v for v in values if v not in valid]
            if unknown:
                raise ValueError(f"Account {name!r} has unknown checks: {unknown} (valid: {valid})")
            if len(set(values)) != len(values):
                raise ValueError(f"Account {name!r} repeats checks: {values}")
            return tuple(AccountCheck(v) for v in values)

        credit_cards_data = (_load_yaml(credit_cards_path) if credit_cards_path else None) or {}
        special = sorted(set(credit_cards_data) & (set(investments_data) | set(INTERNAL_ACCOUNTS)))
        if special:
            raise ValueError(f"Credit card accounts cannot be investment or internal accounts: {special}")
        for name, acc_d in credit_cards_data.items():
            if acc_d.get("is_liquid", True) is not True:
                raise ValueError(f"Credit card account {name!r} is always liquid: is_liquid must be true or missing")

        accounts = {}
        sections = [
            *((AccountType(t), at_accounts, Account) for t, at_accounts in data.items()),
            *((AccountType(t), at_accounts, Account) for t, at_accounts in internal_data.items()),
            (AccountType.LIABILITY, credit_cards_data, CreditCardAccount),
        ]
        for acc_type, at_accounts, acc_cls in sections:
            for name, acc_d in (at_accounts or {}).items():
                if name in accounts:
                    raise ValueError(f"Duplicated account name: {name!r}")
                if name in RESERVED:
                    raise ValueError(f"{name!r} is reserved and cannot be an account name")
                fields = {
                    "name": name,
                    "description": acc_d["description"],
                    "acc_type": acc_type,
                    "currency": currency_of(name, acc_d),
                    "initial_amount": initial_amount_of(name, acc_type, acc_d),
                    "is_liquid": is_liquid_of(name, acc_d) or acc_cls is CreditCardAccount,
                    "initial_date": initial_date_of(name, acc_d),
                    "checks": checks_of(name, acc_d),
                    "internal": name in INTERNAL_ACCOUNTS,
                }
                if name in (INTERMEDIARY, DEBT_ACCOUNT) and not fields["currency"].is_base_currency:
                    raise ValueError(f"The internal account {name!r} must be in the base currency")
                if name in investments_data:
                    investments = InvestmentAccount.investments_from_dict(
                        name, investments_data[name] or {}, (initial_prices or {}).get(name, {})
                    )
                    fields["initial_amount"] = sum(i["total_amount"] for i in investments.values())
                    accounts[name] = InvestmentAccount(**fields, investments=investments)
                elif name == DEBT_ACCOUNT:
                    creditors = DebtAccount.creditors_from_dict(name, creditors_data.get(name) or {}, companies)
                    fields["initial_amount"] = sum(c.amount for c in creditors.values())
                    accounts[name] = DebtAccount(**fields, creditors=creditors)
                else:
                    accounts[name] = acc_cls(**fields)

        unknown = sorted(set(investments_data) - set(accounts))
        if unknown:
            raise ValueError(f"The investments YAML has accounts not in the accounts YAML: {unknown}")
        return accounts


def _parse_date(value, error: str) -> dt.date | None:
    """A date of a YAML: a date, its ISO text or None."""
    if value is None or isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{error}: {value!r}") from None


def previous_month_of_yaml(data: dict | None, today: dt.date | None = None) -> str | None:
    """
    The top-level PREVIOUS_MONTH of the accounts YAML data (YYYY-MM), if any: the last
    past month left out of the movements. It must be before the current month.
    """
    data = data or {}
    if INITIAL_DATE in data:
        raise ValueError(f"The accounts YAML top-level {INITIAL_DATE!r} is replaced by {PREVIOUS_MONTH!r} (YYYY-MM)")
    value = data.get(PREVIOUS_MONTH)
    if value is None:
        return None
    month = parse_month(value, f"{PREVIOUS_MONTH} of the accounts YAML")
    current = (today or dt.date.today()).strftime("%Y-%m")
    if month >= current:
        raise ValueError(f"The {PREVIOUS_MONTH} of the accounts YAML ({month}) must be a past month, before {current}")
    return month


def initial_week(previous_month: str) -> Week:
    """
    The week of the initial movements of the accounts: the last one of the previous
    month (see weeks.Week: of the month of its Thursday). Nothing else happens in it.
    """
    return Week.of_month(previous_month)[-1]


def load_previous_month(path: str) -> str | None:
    """The top-level previous month of the accounts YAML at this path (see previous_month_of_yaml)."""
    return previous_month_of_yaml(_load_yaml(path))


@dataclass
class CreditCardAccount(Account):
    """Liability account of a credit card: what is owed on it. Always liquid."""

    def __post_init__(self):
        super().__post_init__()
        if self.acc_type != AccountType.LIABILITY:
            raise ValueError(f"Credit card account {self.name!r} must be a liability account")
        self.is_liquid = True


@dataclass
class Investment:
    """
    An investment (a bond, a stock, a fund...) held in an investment account. Its
    type is None if unknown: only in the ledger, not in the investments YAML.
    """

    name: str
    type: InvestmentType | None

    def __post_init__(self):
        if self.type is not None:
            self.type = InvestmentType(self.type)


@dataclass
class InvestmentAccount(Account):
    """
    Asset account that holds investments: a dict of investment name to its
    `investment` (Investment), `total_amount` ($) and `total_quantity` (float).
    A `total_quantity` never drops below 0.0. Its `results` are the totals of
    other movements about an investment, such as its dividends or broker fees:
    investment name to base movement name to total ($).
    """

    investments: dict[str, dict] = field(default_factory=dict)
    results: dict[str, dict[str, float]] = field(default_factory=dict)

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
        elif investment.type is None:
            pass  # the type it already has
        elif holding["investment"].type is None:
            holding["investment"] = investment
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

    def add_result(self, name: str, base_movement: str, amount: float) -> None:
        """Adds an amount to the total of a base movement for one of its investments."""
        if name not in self.investments:
            raise ValueError(f"Investment {name!r} is not in the account '{self.name}'")
        totals = self.results.setdefault(name, {})
        totals[base_movement] = totals.get(base_movement, 0.0) + amount

    @staticmethod
    def investments_from_dict(acc_name: str, data: dict, prices: dict[str, float] | None = None) -> dict[str, dict]:
        """
        Investments of an account from its investments YAML dict. An investment is
        either a dict, with its type and initial `quantity`, or just its type (a str),
        with no initial quantity. Its initial amount is that quantity at its initial
        price (of `prices`, investment name to price), required with a quantity.
        """
        prices = prices or {}
        investments = {}
        for inv_name, inv_d in data.items():
            inv_name = str(inv_name)  # YAML keys may be numbers
            if isinstance(inv_d, str):
                inv_d = {"type": inv_d}
            if not isinstance(inv_d, dict) or not inv_d.get("type"):
                raise ValueError(f"Investment {inv_name!r} of the account {acc_name!r} must have a type")
            what = f"Investment {inv_name!r} of the account {acc_name!r}"
            unknown = sorted(set(inv_d) - {"type", "quantity"})
            if "amount" in unknown:
                raise ValueError(
                    f"{what} has an amount: its initial amount is its quantity at its initial price, "
                    f"in the prices CSV ({PREVIOUS_MONTH!r} as its date)"
                )
            if unknown:
                raise ValueError(f"{what} has unknown keys {unknown}")
            try:
                inv_type = InvestmentType(inv_d["type"])
            except ValueError:
                raise ValueError(
                    f"{what} has an unknown type: "
                    f"{inv_d['type']!r} (must be one of {[t.value for t in InvestmentType]})"
                ) from None
            quantity = inv_d.get("quantity", 0.0)
            if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
                raise ValueError(f"{what} quantity must be a number: {quantity!r}")  # noqa: TRY004 (a bad config value)
            if quantity and inv_name not in prices:
                raise ValueError(
                    f"{what} has an initial quantity but no initial price: add it to the prices CSV "
                    f"({PREVIOUS_MONTH!r} as its date)"
                )
            investments[inv_name] = {
                "investment": Investment(name=inv_name, type=inv_type),
                "total_amount": round(float(quantity) * prices.get(inv_name, 0.0), 2),
                "total_quantity": float(quantity),
            }
        return investments


def rounds_to_zero(amount: float) -> bool:
    """Whether an amount shows as 0.00: sums of many amounts leave tiny float residues."""
    return round(amount, 2) == 0


@dataclass
class Creditor:
    """
    Someone we have debts with in a debt account: the amount is positive if we
    owe them (a creditor) and negative if they owe us (a debtor).
    """

    name: str
    amount: float
    kind: CreditorKind = CreditorKind.PERSON
    expected: DebtRole | None = None  # the role they should have, if any
    # Of a service: the columns of the services CSV owed to it, each with its `base_movement`
    # and the `day` of the month it is owed on (None: prorated weekly)
    columns: dict[str, dict] = field(default_factory=dict)

    @property
    def is_creditor(self) -> bool:
        """We owe them (at least a cent)."""
        return self.amount > 0 and not rounds_to_zero(self.amount)

    @property
    def is_debtor(self) -> bool:
        """They owe us (at least a cent)."""
        return self.amount < 0 and not rounds_to_zero(self.amount)

    @property
    def meets_expected(self) -> bool:
        """Not in the opposite role to the expected one (being settled meets either)."""
        if self.expected == DebtRole.CREDITOR:
            return not self.is_debtor
        if self.expected == DebtRole.DEBTOR:
            return not self.is_creditor
        return True


# The expected role of each kind of creditor, unless set in the creditors YAML.
DEFAULT_EXPECTED = {
    CreditorKind.PERSON: None,
    CreditorKind.SERVICE: DebtRole.CREDITOR,
    CreditorKind.COMPANY: DebtRole.DEBTOR,
}


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
        self.unassigned_comments: dict[str, float] = {}  # that debt by comment

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

    def add_debt(self, name: str | None, amount: float, comment: str = "") -> None:
        """
        Adds an amount to what we owe someone (negative: to what they owe us), or
        to the unassigned debt without a name, keeping the movement's comment.
        """
        if name is None:
            self.unassigned += amount
            self.unassigned_comments[comment] = self.unassigned_comments.get(comment, 0.0) + amount
        else:
            self.creditors[name].amount += amount

    def get_creditors(self) -> dict[str, Creditor]:
        """The ones we owe."""
        return {n: c for n, c in self.creditors.items() if c.is_creditor}

    def get_debtors(self) -> dict[str, Creditor]:
        """The ones that owe us."""
        return {n: c for n, c in self.creditors.items() if c.is_debtor}

    @staticmethod
    def _expected_of(acc_name: str, name: str, value) -> DebtRole | None:
        """An expected role from the creditors YAML: null for none."""
        if value is None:
            return None
        try:
            return DebtRole(value)
        except ValueError:
            raise ValueError(
                f"Creditor {name!r} of the account {acc_name!r} has an unknown expected role: {value!r} "
                f"(must be {[r.value for r in DebtRole]} or null)"
            ) from None

    @staticmethod
    def _columns_of(acc_name: str, name: str, value) -> dict[str, dict]:
        """
        The columns of the services CSV owed to a service, from the creditors YAML: column
        name to its base movement, or to a dict of its `base_movement` and the `day` of the
        month it is owed on whole (prorated weekly without one).
        """
        what = f"The columns of the service {name!r} of the account {acc_name!r}"
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"{what} must be a mapping of column: base movement")  # noqa: TRY004 (a bad config value)
        columns = {}
        for column, spec in value.items():
            spec = {"base_movement": spec} if isinstance(spec, str) else spec
            if not isinstance(spec, dict) or not isinstance(spec.get("base_movement"), str):
                raise ValueError(f"{what} must name the base movement of {column!r}")  # noqa: TRY004 (a bad config value)
            extra = sorted(str(k) for k in set(spec) - {"base_movement", "day"})
            if extra:
                raise ValueError(f"{what} have unknown keys {extra} in {column!r}")
            day = spec.get("day")
            if day is not None and (isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 31):
                raise ValueError(f"{what}: the day of {column!r} must be a day of the month, from 1 to 31: {day!r}")
            columns[str(column)] = {"base_movement": spec["base_movement"], "day": day}
        return columns

    def get_kind(self, kind: CreditorKind) -> dict[str, Creditor]:
        """The people, services or companies."""
        return {n: c for n, c in self.creditors.items() if c.kind == kind}

    @staticmethod
    def creditors_from_dict(
        acc_name: str, data: dict, companies: list[str] | None = None
    ) -> dict[str, Creditor]:
        """
        Creditors of an account from its creditors YAML dict, a section per kind
        (`people`, `services`, `companies`) of name: initial debt, or name: a dict
        of `amount` and `expected` (`creditor`, `debtor` or null for none; the
        kind's default if missing), and for a service its `columns` (see
        _columns_of). The `companies` missing from its section are added with no
        initial debt.
        """
        if not isinstance(data, dict):
            raise ValueError(f"The creditors of the account {acc_name!r} must be a mapping")  # noqa: TRY004 (a bad config value)
        unknown = sorted(str(k) for k in set(data) - {k.value for k in CreditorKind})
        if unknown:
            raise ValueError(
                f"The creditors of the account {acc_name!r} have unknown sections {unknown} "
                f"(must be {[k.value for k in CreditorKind]})"
            )
        creditors = {}
        for kind in CreditorKind:
            section = data.get(kind.value) or {}
            if not isinstance(section, dict):
                raise ValueError(f"The {kind.value} of the account {acc_name!r} must be a mapping of name: amount")  # noqa: TRY004 (a bad config value)
            for name, value in section.items():
                name = str(name)  # YAML keys may be numbers
                amount, expected, columns = value, DEFAULT_EXPECTED[kind], {}
                if isinstance(value, dict):
                    keys = {"amount", "expected"} | ({"columns"} if kind == CreditorKind.SERVICE else set())
                    extra = sorted(str(k) for k in set(value) - keys)
                    if extra:
                        raise ValueError(f"Creditor {name!r} of the account {acc_name!r} has unknown keys {extra}")
                    amount = value.get("amount", 0.0 if "columns" in value else None)
                    if "expected" in value:
                        expected = DebtAccount._expected_of(acc_name, name, value["expected"])
                    columns = DebtAccount._columns_of(acc_name, name, value.get("columns"))
                    both = sorted(set(columns) & {c for other in creditors.values() for c in other.columns})
                    if both:
                        raise ValueError(
                            f"The columns {both} of the services CSV are owed to more than one service of {acc_name!r}"
                        )
                if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                    raise ValueError(  # noqa: TRY004 (a bad config value)
                        f"Creditor {name!r} of the account {acc_name!r} amount must be a number: {amount!r}"
                    )
                if name in creditors:
                    raise ValueError(
                        f"Creditor {name!r} of the account {acc_name!r} is in both "
                        f"{creditors[name].kind.value} and {kind.value}"
                    )
                creditors[name] = Creditor(
                    name=name, amount=float(amount), kind=kind, expected=expected, columns=columns,
                )
        for name in companies or []:
            if name not in creditors:
                creditors[name] = Creditor(
                    name=name, amount=0.0, kind=CreditorKind.COMPANY,
                    expected=DEFAULT_EXPECTED[CreditorKind.COMPANY],
                )
            elif creditors[name].kind != CreditorKind.COMPANY:
                raise ValueError(
                    f"Creditor {name!r} of the account {acc_name!r} is a company of the work "
                    f"history, but it is in {creditors[name].kind.value}"
                )
        return creditors


def is_foreign(account: Account) -> bool:
    """An account in another currency, but the equity and investment ones: revalued with it (see store._result_rows)."""
    return (
        not account.currency.is_base_currency and account.acc_type != AccountType.EQUITY
        and not isinstance(account, InvestmentAccount)
    )


# Whether an account is of the kind each special side takes, with its display name.
SPECIAL_SIDES: dict[str, Callable[[Account], bool]] = {
    INVESTMENT: lambda a: isinstance(a, InvestmentAccount),
    CREDIT_CARD: lambda a: isinstance(a, CreditCardAccount),
    FOREIGN_CURRENCY: is_foreign,
}
SPECIAL_LABELS = {INVESTMENT: "Investment", CREDIT_CARD: "Credit card", FOREIGN_CURRENCY: "Foreign currency"}


def special_of(account: Account) -> str | None:
    """INVESTMENT or CREDIT_CARD for investment and credit card accounts, else None."""
    return next((s for s in (INVESTMENT, CREDIT_CARD) if SPECIAL_SIDES[s](account)), None)


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
    debit_special: str | None = None  # one of RESERVED: the debit side takes any such account
    credit_special: str | None = None  # one of RESERVED: the credit side takes any such account

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
        What a side takes: its fixed account name, one of RESERVED (any such account,
        see SPECIAL_SIDES) or None (any account).
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

        The top-level `internal` key has the same structure (its `result_types` next to
        a `transfer` dict): those base movements and result types are internal, reserved
        for pledger itself. Result type ids are shared by internal and other ones.
        """
        data = _load_yaml(path)
        internal_data = dict(data.get(INTERNAL) or {})
        unknown = sorted(set(internal_data) - {"result_types", MovementType.TRANSFER.value})
        if unknown:
            raise ValueError(f"The base movements YAML {INTERNAL!r} key only has 'result_types' and 'transfer', not {unknown}")
        internal = dict(internal_data.get("result_types") or {})

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

        internal_transfers = internal_data.get(MovementType.TRANSFER.value)
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
            assert SPECIAL_SIDES[special](input_acc), (
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
    with the gross salary, its deductions (by the name of their loss base movement,
    like taxes) that take it to the net salary, and when it was received, if it was.
    A month without a company is a gap (no job) and has no salary.
    """

    month: str
    company: str | None = None
    position: str | None = None
    gross: float | None = None
    deductions: dict[str, float] = field(default_factory=dict)
    payment_date: dt.date | None = None

    def __post_init__(self):
        self.month = parse_month(self.month, "month")
        if (self.company is None) != (self.position is None):
            raise ValueError(f"{self.month} must have both a company and a job position, or neither")
        if self.is_gap:
            if self.gross is not None or any(self.deductions.values()) or self.payment_date is not None:
                raise ValueError(f"{self.month} has no job, so it cannot have a salary")
            return
        if self.gross is None:
            raise ValueError(f"{self.month} must have a gross salary")
        if self.gross < 0 or any(d < 0 for d in self.deductions.values()):
            raise ValueError(f"The salary of {self.month} cannot be negative")
        if self.net < -1e-9:
            raise ValueError(f"The deductions of {self.month} cannot be over its gross salary")

    @property
    def is_gap(self) -> bool:
        return self.company is None

    @property
    def total_deductions(self) -> float:
        return sum(self.deductions.values())

    @property
    def net(self) -> float | None:
        """The gross salary minus its deductions, None without a job."""
        return None if self.gross is None else self.gross - self.total_deductions

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
