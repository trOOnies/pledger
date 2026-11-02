"""plans.py: the Budget and Trip tabs. Plans only: neither of them adds movements to the ledger."""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated

from fastapi import APIRouter, Form, Request

from pledger.web.dependencies import StoreDep
from pledger.web.templating import is_partial, templates
from pledger.web.views import (
    budget_form_fields,
    build_budgets,
    build_goals,
    build_trips,
)

router = APIRouter(include_in_schema=False)


def budget_page(request: Request, store, month: str = "", values: dict | None = None, **extra):
    """The Budget tab, or only its content for HTMX. `month` is the current one by default."""
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        month = dt.date.today().isoformat()[:7]
    snapshot = store.load()
    context = {
        **build_budgets(snapshot, store.load_budgets(), month), **build_goals(snapshot, store.load_goals()),
        **budget_form_fields(snapshot), "currency": store.settings.currency, "values": values or {}, **extra,
    }
    return templates.TemplateResponse(request, "_budget.html" if is_partial(request) else "budget.html", context)


def trip_page(request: Request, store, name: str = "", values: dict | None = None, **extra):
    """The Trip tab, or only its content for HTMX."""
    context = {
        **build_trips(store.load(), store.load_trips(), store.load_trip_items(), name),
        "currency": store.settings.currency, "values": values or {}, **extra,
    }
    return templates.TemplateResponse(request, "_trip.html" if is_partial(request) else "trip.html", context)


@router.get("/budget")
def budget(request: Request, store: StoreDep, month: str = ""):
    return budget_page(request, store, month)


@router.post("/budget/budgets")
def save_budget(
    request: Request, store: StoreDep, month: Annotated[str, Form()], base_movement: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
):
    try:
        store.set_budget(month, base_movement, amount)
    except ValueError as e:
        values = {"base_movement": base_movement, "amount": amount}
        return budget_page(request, store, month, values, budget_error=str(e))
    return budget_page(request, store, month, budget_success=f"Budgeted {base_movement} from {month} on")


@router.post("/budget/budgets/stop")
def stop_budget(request: Request, store: StoreDep, month: Annotated[str, Form()], base_movement: Annotated[str, Form()]):
    store.stop_budget(month, base_movement)
    return budget_page(request, store, month, budget_success=f"Stopped budgeting {base_movement} from {month} on")


@router.post("/budget/goals")
def save_goal(
    request: Request, store: StoreDep, month: Annotated[str, Form()] = "", name: Annotated[str, Form()] = "",
    account: Annotated[str, Form()] = "", target: Annotated[str, Form()] = "", due: Annotated[str, Form()] = "",
):
    try:
        store.save_goal(name, account, target, due)
    except ValueError as e:
        values = {"name": name, "account": account, "target": target, "due": due}
        return budget_page(request, store, month, values, goal_error=str(e))
    return budget_page(request, store, month, goal_success=f"Saved the goal {name.strip()}")


@router.post("/budget/goals/delete")
def delete_goal(request: Request, store: StoreDep, name: Annotated[str, Form()], month: Annotated[str, Form()] = ""):
    store.delete_goal(name)
    return budget_page(request, store, month)


@router.get("/trip")
def trip(request: Request, store: StoreDep, trip: str = ""):
    return trip_page(request, store, trip)


@router.post("/trip/trips")
def save_trip(
    request: Request, store: StoreDep, name: Annotated[str, Form()] = "", start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "", base_movement: Annotated[str, Form()] = "",
):
    try:
        store.save_trip(name, start, end, base_movement)
    except ValueError as e:
        values = {"name": name, "start": start, "end": end, "base_movement": base_movement}
        return trip_page(request, store, "", values, trip_error=str(e))
    return trip_page(request, store, name.strip(), trip_success=f"Saved the trip {name.strip()}")


@router.post("/trip/trips/delete")
def delete_trip(request: Request, store: StoreDep, name: Annotated[str, Form()]):
    store.delete_trip(name)
    return trip_page(request, store)


@router.post("/trip/items")
def add_item(
    request: Request, store: StoreDep, trip: Annotated[str, Form()], category: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "", amount: Annotated[str, Form()] = "", currency: Annotated[str, Form()] = "",
):
    try:
        store.add_trip_item(trip, category, description, amount, currency)
    except ValueError as e:
        values = {"category": category, "description": description, "amount": amount, "currency": currency}
        return trip_page(request, store, trip, values, item_error=str(e))
    # Keep the currency and category to speed up entering similar expenses.
    return trip_page(request, store, trip, {"category": category, "currency": currency}, item_success="Added the expense")


@router.post("/trip/items/delete")
def delete_item(request: Request, store: StoreDep, trip: Annotated[str, Form()], item: Annotated[str, Form()]):
    store.delete_trip_item(item)
    return trip_page(request, store, trip)
