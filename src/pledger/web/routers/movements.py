"""movements.py"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from pledger.web.dependencies import StoreDep
from pledger.web.schemas import MovementIn
from pledger.web.store import StaleRowError
from pledger.web.views import build_view

router = APIRouter(prefix="/api/movements", tags=["movements"])


@router.get("")
def list_movements(store: StoreDep, month: str = "", account: str = "", initial: bool = False):
    return build_view(store.load(), month, account, initial)["movements"]


@router.post("", status_code=201)
def create_movement(store: StoreDep, movement: MovementIn):
    try:
        store.add(**movement.model_dump())
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
