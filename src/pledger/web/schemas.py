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
    initial_amount: float
    currency: str = ""  # the base currency if empty
    is_liquid: bool = False
    initial_date: str = ""  # the first date of the ledger if empty


class ResultTypeIn(BaseModel):
    name: str
    long_name: str
    description: str


class BaseMovementIn(BaseModel):
    name: str
    type: MovementType
    description: str
    debit: str | None = None
    credit: str | None = None
    result_type: str | None = None  # required for gains and losses
