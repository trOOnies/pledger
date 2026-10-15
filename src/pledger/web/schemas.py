"""schemas.py"""

from __future__ import annotations

from pydantic import BaseModel

from pledger.enums import AccountType, MovementType


class MovementIn(BaseModel):
    move_date: str
    base_movement: str
    acc_debit: str
    acc_credit: str
    amount: float
    comment: str = ""


class AccountIn(BaseModel):
    name: str
    type: AccountType
    description: str


class BaseMovementIn(BaseModel):
    name: str
    type: MovementType
    description: str
    debit: str | None = None
    credit: str | None = None
    result_type: str | None = None  # required for gains and losses
