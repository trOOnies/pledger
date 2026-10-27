"""debt.py: movements of the debt base movements, kept out of the ledger API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pledger.web.dependencies import StoreDep
from pledger.web.schemas import DebtMovementIn

router = APIRouter(prefix="/api/debt", tags=["debt"])


@router.post("/movements", status_code=201)
def create_debt_movement(store: StoreDep, movement: DebtMovementIn):
    """Added to the ledger CSV like the others, and deleted from /api/movements."""
    try:
        store.add_debt_movement(**movement.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True}
