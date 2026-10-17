"""Shared fixtures for pledger tests."""

from __future__ import annotations

import pandas as pd
import pytest

from pledger.enums import AccountType
from pledger.models import Account, Currency, MovementBase, ResultType

CURRENCIES_YAML = """\
base_currency:
  name: ARS
  description: Pesos
other_currencies:
  - name: USD
    description: Dollar
"""

USD_CSV = """\
date;unit_value_in_base_currency
2026-01-01;1000
"""

ACCOUNTS_YAML = """\
asset:
  Ca:
    description: Cash
    is_liquid: true
    initial_amount: 0.0
  Bk:
    description: Bank account
    currency: USD
    is_liquid: true
    initial_amount: 100.5
    initial_date: 2025-12-31
liability:
  Cc:
    description: Credit card
    initial_amount: 0
equity:
  Eq:
    description: Equity
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
        "Eq": Account("Eq", "Equity", AccountType.EQUITY, ars, 0.0),
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
    (tmp_path / "currencies").mkdir(exist_ok=True)
    (tmp_path / "currencies" / "USD.csv").write_text(USD_CSV, encoding="utf-8")
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
