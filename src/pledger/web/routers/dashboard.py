"""
dashboard.py: the slots of the Dashboard tab: putting a widget in a free slot, freeing a slot,
and moving a widget to another slot (swapping it with the one there). Each one answers with the
slots, rendered again.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from pledger.web.dependencies import StoreDep
from pledger.web.store import DASHBOARD_SLOTS
from pledger.web.templating import templates
from pledger.web.views import WIDGETS, build_dashboard, valid_dashboard

router = APIRouter(prefix="/dashboard", tags=["pages"], default_response_class=HTMLResponse)

Kind = Annotated[str, Form()]
Slot = Annotated[int, Form()]


def _check_slot(kind: str, slot: int) -> None:
    if kind not in DASHBOARD_SLOTS:
        raise HTTPException(status_code=400, detail=f"No {kind!r} slots in the dashboard")
    if not 0 <= slot < DASHBOARD_SLOTS[kind]:
        raise HTTPException(status_code=400, detail=f"No slot {slot + 1} of the {kind} of the dashboard")


def _slots(request: Request, store, dashboard: dict):
    store.save_dashboard(dashboard)
    context = build_dashboard(store.load(), dashboard)
    return templates.TemplateResponse(request, "_dashboard.html", context)


@router.post("/place")
def place(request: Request, store: StoreDep, kind: Kind, slot: Slot, widget: Annotated[str, Form()]):
    """Put a widget in a free slot."""
    _check_slot(kind, slot)
    dashboard = valid_dashboard(store.load_dashboard())
    if widget not in WIDGETS[kind]:
        raise HTTPException(status_code=400, detail=f"No {widget!r} to put in the {kind} of the dashboard")
    if widget in dashboard[kind]:
        raise HTTPException(status_code=400, detail=f"{WIDGETS[kind][widget][0]} is already in the dashboard")
    if dashboard[kind][slot] is not None:
        raise HTTPException(status_code=400, detail="That slot is not free: free it first")
    dashboard[kind][slot] = widget
    return _slots(request, store, dashboard)


@router.post("/clear")
def clear(request: Request, store: StoreDep, kind: Kind, slot: Slot):
    """Free a slot."""
    _check_slot(kind, slot)
    dashboard = valid_dashboard(store.load_dashboard())
    dashboard[kind][slot] = None
    return _slots(request, store, dashboard)


@router.post("/move")
def move(request: Request, store: StoreDep, kind: Kind, source: Slot, target: Slot):
    """Move the widget of a slot to another one, swapping it with the widget there, if any."""
    _check_slot(kind, source)
    _check_slot(kind, target)
    dashboard = valid_dashboard(store.load_dashboard())
    slots = dashboard[kind]
    slots[source], slots[target] = slots[target], slots[source]
    return _slots(request, store, dashboard)
