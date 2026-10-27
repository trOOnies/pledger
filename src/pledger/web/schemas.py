"""schemas.py"""

from __future__ import annotations

from pydantic import BaseModel

from pledger.enums import AccountCheck, AccountType, InvestmentType, MovementType


class MovementIn(BaseModel):
    move_date: str
    base_movement: str
    acc_debit: str
    acc_credit: str
    amount: float
    comment: str = ""


class InvestMovementIn(MovementIn):
    investment: str = ""  # required with an INVESTMENT side
    quantity: float | None = None  # required to buy, sell and split
    investment_type: InvestmentType | None = None  # for an investment new to its account


class ConversionIn(BaseModel):
    move_date: str
    base_movement: str  # a transfer
    acc_debit: str
    acc_credit: str
    amount_base: float  # in the base currency
    amount_other: float  # in the currency of the account not in the base one
    comment: str = ""


class AccountIn(BaseModel):
    name: str
    type: AccountType
    description: str
    initial_amount: float
    currency: str = ""  # the base currency if empty
    is_liquid: bool = False
    initial_date: str = ""  # the first date of the ledger if empty
    checks: list[AccountCheck] = []  # on its daily balance


class WorkMonthIn(BaseModel):
    month: str  # YYYY-MM
    company: str = ""  # empty for a month without a job
    position: str = ""
    gross: float | None = None
    deductions: dict[str, float] = {}  # by base movement, taking the gross salary to the net one
    payment_date: str = ""  # empty if not received yet


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
