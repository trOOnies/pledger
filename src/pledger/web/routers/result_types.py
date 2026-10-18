"""result_types.py"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pledger.web.dependencies import StoreDep
from pledger.web.schemas import ResultTypeIn

router = APIRouter(prefix="/api/result-types", tags=["result-types"])


@router.get("")
def list_result_types(store: StoreDep):
    result_types = store.load().result_types
    return [
        {
            "id": rt.id,
            "name": rt.name,
            "long_name": rt.long_name,
            "description": rt.description,
            "internal": rt.internal,
        }
        for rt in (result_types[i] for i in sorted(result_types))
    ]


@router.post("", status_code=201)
def create_result_type(store: StoreDep, result_type: ResultTypeIn):
    try:
        store.add_result_type(result_type.name, result_type.long_name, result_type.description)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True}
