"""enums.py"""

from __future__ import annotations

from enum import Enum
from typing import Any


class LedgerSide(str, Enum):
    """Ledger side."""
    DEBIT = "debit"
    CREDIT = "credit"


class AccountType(str, Enum):
    """Type of account based on equity side."""
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"


class InvestmentType(str, Enum):
    """Type of investment."""
    PUBLIC_BOND = "public_bond"
    PRIVATE_BOND = "private_bond"
    MUTUAL_FUND = "mutual_fund"
    ETF = "etf"
    FOREX = "forex"
    STOCK = "stock"
    DERIVATIVE = "derivative"
    FIXED_TERM = "fixed_term"


class CreditorKind(str, Enum):
    """Kind of creditor or debtor in a debt account: its section in the creditors YAML."""
    PERSON = "people"
    SERVICE = "services"
    COMPANY = "companies"


class MovementType(str, Enum):
    """Type of movement based on equity side."""
    GAIN = "gain"
    LOSS = "loss"
    TRANSFER = "transfer"

    @classmethod
    def from_accounts(
        cls,
        acc_debit: Any | None,
        acc_credit: Any | None,
    ) -> MovementType:
        """Get MovementType from debit and credit Accounts"""
        c_loss = (acc_debit is not None) and (acc_debit.acc_type == AccountType.EQUITY)
        c_gain = (acc_credit is not None) and (acc_credit.acc_type == AccountType.EQUITY)

        if not c_loss and not c_gain:
            return cls.TRANSFER

        return cls.GAIN if c_gain else cls.LOSS
