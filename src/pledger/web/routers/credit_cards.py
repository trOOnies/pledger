"""credit_cards.py: movements of the card payment base movements, kept out of the ledger API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from pledger.web.dependencies import StoreDep
from pledger.web.schemas import MovementIn
from pledger.web.store import StaleRowError

router = APIRouter(prefix="/api/credit-cards", tags=["credit cards"])


@router.post("/payments", status_code=201)
def create_card_payment(store: StoreDep, movement: MovementIn):
    """Added to the ledger CSV like the others."""
    try:
        store.add(**movement.model_dump(), card_payment=True)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True}


@router.delete("/payments/{index}")
def delete(store: StoreDep, index: int, fingerprint: Annotated[str | None, Query()] = None):
    """Deleted from the ledger CSV, but only here."""
    try:
        store.delete(index, fingerprint, card_payment=True)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True}
