"""enums.py"""

from __future__ import annotations

from enum import Enum


class AccountType(Enum, str):
    """Type of account based on equity side."""
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"


class MovementType(Enum, str):
    """Type of movement based on equity side."""
    GAIN = "gain"
    LOSS = "loss"
    TRANSFER = "transfer"

    @classmethod
    def from_accounts(
        cls,
        acc_debit: AccountType,
        acc_credit: AccountType,
    ) -> MovementType:
        c1 = acc_debit == AccountType.EQUITY
        c2 = acc_debit == AccountType.EQUITY

        if not c1 and not c2:
            ...

        return ...
