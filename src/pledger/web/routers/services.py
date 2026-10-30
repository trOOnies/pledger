"""services.py: the monthly values of the services of the debt account."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pledger.web.dependencies import StoreDep
from pledger.web.schemas import ServiceValueIn
from pledger.web.views import build_services

router = APIRouter(prefix="/api/services", tags=["services"])


@router.get("")
def list_services(store: StoreDep, service: str = ""):
    """The services with their columns, the monthly values (newest first) and the movements owed to them."""
    data = build_services(store.load(), service)
    return {k: data[k] for k in ("services", "columns", "values", "movements")}


@router.put("")
def set_service_value(store: StoreDep, value: ServiceValueIn):
    """Set the value of a service in a month of the services CSV, replacing it; none clears it."""
    try:
        store.set_service_value(value.month, value.service, "" if value.amount is None else value.amount)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True}
