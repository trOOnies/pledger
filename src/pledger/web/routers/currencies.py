"""currencies.py: the weekly values of the other currencies, in the currencies CSV."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pledger.web.dependencies import StoreDep
from pledger.web.schemas import CurrencyWeekIn
from pledger.web.views import currency_weeks

router = APIRouter(prefix="/api/currencies", tags=["currencies"])


@router.get("")
def list_currency_weeks(store: StoreDep):
    """
    The weekly values of the other currencies in the base currency (newest first, a cell
    per currency of `week_currencies`), and the past weeks of each one without a known value.
    """
    return currency_weeks(store.load())


@router.put("")
def save_currency_week(store: StoreDep, week: CurrencyWeekIn):
    """Save the values of a date of the currencies CSV; the Foreign currency movements follow from them."""
    try:
        date = store.save_currency_week(week.date, {k: str(v) for k, v in week.values.items()})
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True, "date": date}
