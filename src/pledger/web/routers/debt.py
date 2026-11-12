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
    """
    Added to the ledger CSV like the others, `amount` in the currency of the other account and
    `amount_debt` in that of the debt account, when they are different.
    """
    fields = movement.model_dump()
    fields["amount_debt"] = "" if movement.amount_debt is None else movement.amount_debt
    try:
        difference = store.add_debt_movement(**fields)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True, "difference": dict(zip(("base_movement", "amount"), difference)) if difference else None}


@router.delete("/movements/{index}")
def delete(store: StoreDep, index: int, fingerprint: Annotated[str | None, Query()] = None):
    """Deleted from the ledger CSV, but only here."""
    try:
        store.delete_debt_movement(index, fingerprint)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True}
