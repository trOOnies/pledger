"""base_movements.py"""

from __future__ import annotations

from fastapi import APIRouter

from pledger.web.dependencies import StoreDep

router = APIRouter(prefix="/api/base-movements", tags=["base-movements"])


@router.get("")
def list_base_movements(store: StoreDep):
    return [
        {
            "name": b.name,
            "description": b.description,
            "type": b.move_type.value,
            "debit": b.acc_debit.name if b.acc_debit else None,
            "credit": b.acc_credit.name if b.acc_credit else None,
        }
        for b in store.load().base_movements.values()
    ]
