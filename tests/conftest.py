"""Shared fixtures for pledger tests."""

from __future__ import annotations

import pytest

from pledger.enums import AccountType
from pledger.models import Account, MovementBase

ACCOUNTS_YAML = """\
asset:
  Ca:
    description: Cash
  Bk:
    description: Bank account
liability:
  Cc:
    description: Credit card
equity:
  Eq:
    description: Equity
"""

BASE_MOVEMENTS_YAML = """\
gain:
  Salary:
    description: Monthly salary
    debit: Bk
  Gift:
    description: Any gift
loss:
  Food:
    description: Groceries
  Card fee:
    description: Credit card fee
    credit: Cc
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
def accounts() -> dict[str, Account]:
    return {
        "Ca": Account("Ca", "Cash", AccountType.ASSET),
        "Bk": Account("Bk", "Bank account", AccountType.ASSET),
        "Cc": Account("Cc", "Credit card", AccountType.LIABILITY),
        "Eq": Account("Eq", "Equity", AccountType.EQUITY),
    }


@pytest.fixture
def base_movements(accounts) -> dict[str, MovementBase]:
    eq = accounts["Eq"]
    return {
        "Salary": MovementBase("Salary", "", accounts["Bk"], eq),
        "Gift": MovementBase("Gift", "", None, eq),
        "Food": MovementBase("Food", "", eq, None),
        "Card fee": MovementBase("Card fee", "", eq, accounts["Cc"]),
        "Withdraw": MovementBase("Withdraw", "", accounts["Ca"], accounts["Bk"]),
        "Move": MovementBase("Move", "", None, None),
    }


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
