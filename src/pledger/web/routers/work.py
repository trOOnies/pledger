"""work.py"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pledger.web.dependencies import StoreDep
from pledger.web.schemas import WorkMonthIn

router = APIRouter(prefix="/api/work", tags=["work"])


@router.get("/companies")
def list_companies(store: StoreDep):
    return [
        {
            "name": c.name,
            "start": c.start,
            "end": c.end,
            "positions": [
                {"name": p.name, "start": p.start, "end": p.end, "months": len(p.months)}
                for p in c.positions.values()
            ],
        }
        for c in store.load_work().companies.values()
    ]


@router.get("/months")
def list_months(store: StoreDep):
    return [
        {
            "month": m.month,
            "company": m.company,
            "position": m.position,
            "gross": m.gross,
            "deductions": m.deductions,
            "net": m.net,
            "payment_date": m.payment_date.isoformat() if m.payment_date else None,
        }
        for m in store.load_work().months
    ]


@router.put("/months")
def save_month(store: StoreDep, month: WorkMonthIn):
    """Add a month (after the last one or before the first one) or replace an existing one."""
    try:
        created = store.save_month(
            month.month, month.company, month.position,
            "" if month.gross is None else month.gross,
            month.deductions,
            month.payment_date,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True, "created": created}
