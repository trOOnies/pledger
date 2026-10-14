"""schemas.py"""

from __future__ import annotations

from pydantic import BaseModel


class MovementIn(BaseModel):
    move_date: str
    base_movement: str
    acc_debit: str
    acc_credit: str
    amount: float
    comment: str = ""
