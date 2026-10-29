"""Shared helpers of the tests of pledger.web: data files to add, and parsing the pages."""

import re
from dataclasses import replace
from pathlib import Path

from pledger.web.store import (
    LedgerStore,
    Settings,
)

HTMX = {"HX-Request": "true"}


def with_internal(path: Path, result_types: str = "", transfers: str = "") -> None:
    """Add these internal result types and transfers (YAML entries of their sections) to the base movements YAML."""
    text = path.read_text()
    text = text.replace("  result_types:\n", "  result_types:\n" + result_types, 1)
    path.write_text(text.replace("  transfer:\n", "  transfer:\n" + transfers, 1))


PRICES_HEADER = "date;account;investment;price\n"


def with_prices(settings: Settings, text: str) -> Settings:
    """The settings, with a prices CSV of these rows next to the accounts YAML."""
    path = settings.accounts_path.parent / "prices.csv"
    path.write_text(PRICES_HEADER + text, encoding="utf-8")
    return replace(settings, prices_path=path)


def movement_ids(text: str) -> list[str]:
    """The ids in the ID column of the tables of movements of a page, in order."""
    return re.findall(r'<td class="nowrap muted small" data-sort="([^"]+)">', text)


def rows(settings):
    return LedgerStore(settings).load().rows


def card_labels(html: str) -> list[str]:
    return [c.split("</span>")[0].split(" <small>")[0] for c in html.split('<span class="label">')[1:]]


def with_conversion_bases(settings: Settings) -> Settings:
    """The settings, with the internal transfers of the conversions."""
    path = settings.base_movements_path
    with_internal(path, transfers=(
        "    Foreign currency - Bought:\n      description: Pesos to dollars\n      debit: FOREIGN CURRENCY\n"
        "    Foreign currency - Sold:\n      description: Dollars to pesos\n      credit: FOREIGN CURRENCY\n"
    ))
    return settings


MOVEMENT = {"move_date": "2026-03-01", "base_movement": "Gift", "acc_debit": "Ca",
            "acc_credit": "Eq", "amount": "100"}


SERVICES_HEADER = "month;Rent;Expenses;Gas\n"


WORK_CSV = """\
Month;Company;Position;Work - Salary Main;Tax
2025-10;Acme;Acme Jr;1000.00;200.00
2025-11;Acme;Acme Sr;1500.50;300.50
2025-12;;;;
2026-01;Globex;GX Dev;2000.00;400.00"""


def with_salaries(settings, debit="Ca"):
    """
    The settings, with the base movement of the salaries of the work history debiting
    that account, and the Tax loss of their deductions.
    """
    path = settings.base_movements_path
    salary = f"        Work - Salary Main:\n          description: Main salary\n          debit: {debit}\n"
    text = path.read_text().replace("        Salary:\n", salary + "        Salary:\n", 1)
    path.write_text(text.replace("        Food:\n", "        Tax: Income taxes\n        Food:\n", 1))
    return settings


# The internal result type and transfers of the investments (see with_internal)
INVEST_YAML = (
    "    Invest:\n      id: 4\n      long_name: Investments\n      description: d\n"
    "      base_movements:\n        gain:\n          Capital gains:\n            description: d\n"
    "            debit: INVESTMENT\n          Dividends: d\n        loss:\n          Broker fees: d\n"
    "          Capital losses:\n            description: d\n            credit: INVESTMENT\n"
)


INVEST_TRANSFERS_YAML = (
    "    Investment - Bought:\n      description: d\n      debit: INVESTMENT\n"
    "    Investment - Sold:\n      description: d\n      credit: INVESTMENT\n"
    "    Adjust: Set by pledger\n"
)


# The internal transfers of the Debt tab (see with_internal)
DEBT_YAML = (
    "    Debt - Incurred:\n      description: d\n      credit: De\n"
    "    Debt - Paid:\n      description: d\n      debit: De\n"
    "    Loan - Given:\n      description: d\n      debit: De\n"
    "    Loan - Collected:\n      description: d\n      credit: De\n"
)


INVESTMENT_HEADER = "move_date;base_movement;acc_debit;amount;acc_credit;quantity;investment;comment\n"


def investment_rows(settings):
    return LedgerStore(settings).load().investments


# The internal transfers of the Credit Cards tab (see with_internal)
CARD_PAYMENT_YAML = (
    "    Credit card - Payment:\n      description: d\n      debit: CREDIT CARD\n"
    "    Credit card - Surplus:\n      description: d\n      credit: CREDIT CARD\n"
)


CREDIT_CARDS_YAML = """\
Vi:
  long_name: Visa (L)
  description: Visa card
  initial_amount: 100
MM:
  description: Mastercard
  initial_amount: 0
"""


CORRECTION_YAML = INVEST_YAML.replace("          Dividends: d\n", "          Dividends: d\n          Correction (G): c\n").replace(
    "          Broker fees: d\n", "          Broker fees: d\n          Correction (L): c\n"
)
