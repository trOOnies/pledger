"""debt.py: movements of the debt base movements, kept out of the ledger API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from pledger.web.dependencies import StoreDep
from pledger.web.schemas import DebtMovementIn
from pledger.web.store import StaleRowError

router = APIRouter(prefix="/api/debt", tags=["debt"])


@router.post("/movements", status_code=201)
def create_debt_movement(store: StoreDep, movement: DebtMovementIn):
    """Added to the ledger CSV like the others."""
    try:
        store.add_debt_movement(**movement.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True}


@router.delete("/movements/{index}")
def delete(store: StoreDep, index: int, fingerprint: Annotated[str | None, Query()] = None):
    """Deleted from the ledger CSV, but only here."""
    try:
        store.delete(index, fingerprint, debt=True)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True}
