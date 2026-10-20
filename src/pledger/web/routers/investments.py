"""investments.py: movements of the Invest base movements, kept out of the ledger API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from pledger.web.dependencies import StoreDep
from pledger.web.schemas import InvestMovementIn
from pledger.web.store import StaleRowError
from pledger.web.views import build_invest_movements

router = APIRouter(prefix="/api/investments/movements", tags=["investments"])


@router.get("")
def list_investment_movements(store: StoreDep):
    return build_invest_movements(store.load())


@router.post("", status_code=201)
def create_investment_movement(store: StoreDep, movement: InvestMovementIn):
    fields = movement.model_dump()
    fields["quantity"] = "" if movement.quantity is None else movement.quantity
    fields["investment_type"] = movement.investment_type.value if movement.investment_type else ""
    try:
        store.add(**fields, invest=True)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True}


@router.delete("/{index}")
def delete_investment_movement(
    store: StoreDep, index: int, fingerprint: Annotated[str | None, Query()] = None
):
    try:
        store.delete(index, fingerprint, invest=True)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True}
