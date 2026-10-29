"""Shared fixtures for pledger tests."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from pledger.enums import AccountType
from pledger.models import Account, Currency, MovementBase, ResultType
from pledger.web.app import create_app
from pledger.web.store import LOCAL_HOSTS, Settings

CURRENCIES_YAML = """\
base_currency:
  name: ARS
  description: Pesos
other_currencies:
  - name: USD
    description: Dollar
"""

CURRENCIES_CSV = """\
date;USD
2026-01-01;1000
"""

BASE_CURRENCY_CSV = """\
month;monthly_inflation
2025-12;2.00%
2026-01;3.00%
"""

# The mandatory internal accounts, first in every accounts YAML of the tests: in flow
# style, so that replacing the lines of the other sections leaves them alone
INTERNAL_ACCOUNTS_YAML = """\
internal:
  asset: {Fi: {description: Intermediary, initial_amount: 0, checks: [net_zero]}}
  liability: {De: {description: Debts}}
  equity: {Eq: {description: Equity}}
"""

ACCOUNTS_YAML = INTERNAL_ACCOUNTS_YAML + """\
asset:
  Ca:
    description: Cash
    is_liquid: true
    initial_amount: 0.0
  Bk:
    description: Bank account
    currency: USD
    is_liquid: true
    initial_amount: 0.1005  # 100.50 ARS at 1,000
    initial_date: 2025-12-31
liability:
  Cc:
    description: Credit card
    initial_amount: 0
"""

BASE_MOVEMENTS_YAML = """\
result_types:
  Op:
    id: 1
    long_name: Operative
    description: Operative results
    base_movements:
      gain:
        Salary:
          description: Monthly salary
          debit: Bk
      loss:
        Food:
          description: Groceries
        Card fee:
          description: Credit card fee
          credit: Cc
  Gifts:
    id: 2
    long_name: Gifts
    description: Results because of gifts
    base_movements:
      gain:
        Gift:
          description: Any gift
transfer:
  Withdraw:
    description: Bank to cash
    debit: Ca
    credit: Bk
  Move:
    description: Free transfer
"""

LEDGER_CSV = """\
move_date;base_movement;acc_debit;amount;acc_credit;comment
2026-01-01;Salary;Bk;"1,000.50";Eq;
2026-01-02;Withdraw;Ca;200;Bk;
2026-01-03;Food;Eq;50.25;Ca;
2026-01-04;Gift;Ca;10;Eq;
"""


@pytest.fixture
def currencies() -> dict[str, Currency]:
    return {
        "ARS": Currency("ARS", "Pesos", None),
        "USD": Currency("USD", "Dollar", pd.Series([1000.0], index=[pd.Timestamp("2026-01-01")])),
    }


@pytest.fixture
def accounts(currencies) -> dict[str, Account]:
    ars, usd = currencies["ARS"], currencies["USD"]
    return {
        "Ca": Account("Ca", "Cash", AccountType.ASSET, ars, 0.0, is_liquid=True),
        "Bk": Account("Bk", "Bank account", AccountType.ASSET, usd, 100.5, is_liquid=True),
        "Cc": Account("Cc", "Credit card", AccountType.LIABILITY, ars, 0.0),
        "Eq": Account("Eq", "Equity", AccountType.EQUITY, ars, 0.0, internal=True),
    }


@pytest.fixture
def result_types() -> dict[int, ResultType]:
    return {
        1: ResultType(1, "Op", "Operative", "Operative results"),
        2: ResultType(2, "Gifts", "Gifts", "Results because of gifts"),
    }


@pytest.fixture
def base_movements(accounts, result_types) -> dict[str, MovementBase]:
    eq = accounts["Eq"]
    op, gifts = result_types[1], result_types[2]
    return {
        "Salary": MovementBase("Salary", "", accounts["Bk"], eq, op),
        "Gift": MovementBase("Gift", "", None, eq, gifts),
        "Food": MovementBase("Food", "", eq, None, op),
        "Card fee": MovementBase("Card fee", "", eq, accounts["Cc"], op),
        "Withdraw": MovementBase("Withdraw", "", accounts["Ca"], accounts["Bk"], None),
        "Move": MovementBase("Move", "", None, None, None),
    }


@pytest.fixture
def currencies_yaml(tmp_path):
    (tmp_path / "currencies.csv").write_text(CURRENCIES_CSV, encoding="utf-8")
    (tmp_path / "base_currency.csv").write_text(BASE_CURRENCY_CSV, encoding="utf-8")
    path = tmp_path / "currencies.yaml"
    path.write_text(CURRENCIES_YAML, encoding="utf-8")
    return str(path)


@pytest.fixture
def accounts_yaml(tmp_path):
    path = tmp_path / "accounts.yaml"
    path.write_text(ACCOUNTS_YAML, encoding="utf-8")
    return str(path)


@pytest.fixture
def base_movements_yaml(tmp_path):
    path = tmp_path / "base_movements.yaml"
    path.write_text(BASE_MOVEMENTS_YAML, encoding="utf-8")
    return str(path)


@pytest.fixture
def ledger_csv(tmp_path):
    path = tmp_path / "ledger.csv"
    path.write_text(LEDGER_CSV, encoding="utf-8")
    return str(path)


# The internal base movements required to load the ledger: an empty `transfer` section
# and the `result_types` one, with Init last (see with_internal)
INITIAL_YAML = (
    "internal:\n  transfer:\n  result_types:\n    Init:\n      id: 3\n      long_name: Initial\n      description: d\n"
    "      base_movements:\n        gain:\n          Initial (G): Initial balance\n"
    "        loss:\n          Initial (L): Initial balance\n"
)


# The internal base movements of the results of the accounts in other currencies,
# required once one is revalued; appended to the internal `result_types` of INITIAL_YAML
RESULT_YAML = (
    "    Expo:\n      id: 4\n      long_name: Exposition\n      description: d\n"
    "      base_movements:\n        gain:\n          Foreign currency (G):\n            description: d\n"
    "            debit: FOREIGN CURRENCY\n        loss:\n          Foreign currency (L):\n"
    "            description: d\n            credit: FOREIGN CURRENCY\n"
)


@pytest.fixture
def settings(accounts_yaml, base_movements_yaml, ledger_csv, currencies_yaml) -> Settings:
    # At the top, so that later appends extend the `transfer` section
    path = Path(base_movements_yaml)
    path.write_text(INITIAL_YAML + path.read_text())
    return Settings(
        Path(accounts_yaml), Path(base_movements_yaml), Path(ledger_csv), Path(currencies_yaml),
        allowed_hosts=(*LOCAL_HOSTS, "testserver"),  # the host of the TestClient
    )


@pytest.fixture
def client(settings) -> TestClient:
    return TestClient(create_app(settings))


@pytest.fixture
def revalued(settings) -> Settings:
    """The settings, with the base movements to revalue the accounts in other currencies."""
    path = settings.base_movements_path
    path.write_text(path.read_text().replace(INITIAL_YAML, INITIAL_YAML + RESULT_YAML, 1))
    return settings
