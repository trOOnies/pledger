"""movements.py"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from pledger.web.dependencies import StoreDep
from pledger.web.schemas import LedgerMovementIn
from pledger.web.store import StaleRowError
from pledger.web.views import build_view

router = APIRouter(prefix="/api/movements", tags=["movements"])


@router.get("")
def list_movements(
    store: StoreDep, month: str = "", account: str = "", initial: bool = False, base: str = "",
    q: str = "",
):
    return build_view(store.load(), month, account, initial, base, q)["movements"]


@router.post("", status_code=201)
def create_movement(store: StoreDep, movement: LedgerMovementIn):
    try:
        values = movement.model_dump()
        installments = values.pop("installments") or ""
        store.add_ledger_movement(**values, installments=installments)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True}


@router.put("/{index}")
def edit_movement(
    store: StoreDep, index: int, movement: LedgerMovementIn, fingerprint: Annotated[str | None, Query()] = None
):
    try:
        values = movement.model_dump()
        installments = values.pop("installments") or ""
        store.edit(index, fingerprint, **values, installments=installments)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True}


@router.delete("/{index}")
def delete_movement(
    store: StoreDep, index: int, fingerprint: Annotated[str | None, Query()] = None
):
    try:
        store.delete(index, fingerprint)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True}
