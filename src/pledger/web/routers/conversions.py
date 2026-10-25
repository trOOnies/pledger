"""conversions.py: movements of the conversions CSV, kept out of the ledger API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from pledger.web.dependencies import StoreDep
from pledger.web.schemas import ConversionIn
from pledger.web.store import StaleRowError
from pledger.web.views import build_conversions

router = APIRouter(prefix="/api/conversions", tags=["conversions"])


@router.get("")
def list_conversions(store: StoreDep):
    return build_conversions(store.load())


@router.post("", status_code=201)
def create_conversion(store: StoreDep, conversion: ConversionIn):
    try:
        store.add_conversion(**conversion.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True}


@router.delete("/{index}")
def delete_conversion(
    store: StoreDep, index: int, fingerprint: Annotated[str | None, Query()] = None
):
    try:
        store.delete_conversion(index, fingerprint)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True}
