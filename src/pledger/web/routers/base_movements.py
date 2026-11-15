"""base_movements.py"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pledger.enums import LedgerSide
from pledger.web.dependencies import StoreDep
from pledger.web.schemas import BaseMovementIn

router = APIRouter(prefix="/api/base-movements", tags=["base-movements"])


@router.get("")
def list_base_movements(store: StoreDep):
    return [
        {
            "name": b.name,
            "description": b.description,
            "type": b.move_type.value,
            "result_type": b.result_type.name if b.result_type else None,
            "debit": b.side(LedgerSide.DEBIT),
            "credit": b.side(LedgerSide.CREDIT),
            "internal": b.internal,
        }
        for b in store.load().base_movements.values()
    ]


@router.post("", status_code=201)
def create_base_movement(store: StoreDep, base: BaseMovementIn):
    try:
        store.add_base_movement(
            base.name, base.type.value, base.description,
            base.debit or "", base.credit or "", base.result_type or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True}
