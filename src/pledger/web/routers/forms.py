"""forms.py: HTMX endpoints behind the movement and setup forms."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from pledger.web.dependencies import StoreDep
from pledger.web.store import LedgerStore, StaleRowError
from pledger.web.templating import money, remembered_month, render_form, templates
from pledger.web.views import (
    account_fields,
    base_fields,
    base_sides,
    build_accounts,
    months_of,
    setup_context,
)

router = APIRouter(tags=["forms"], default_response_class=HTMLResponse)


@router.get("/form/bases")
def form_bases(request: Request, store: StoreDep, move_type: str = ""):
    return templates.TemplateResponse(
        request,
        "_base_fields.html",
        {**base_fields(store.load(), move_type), "values": {}},
    )


@router.get("/form/accounts")
def form_accounts(request: Request, store: StoreDep, base_movement: str = ""):
    snapshot = store.load()
    return templates.TemplateResponse(
        request,
        "_account_fields.html",
        {"fields": account_fields(snapshot, base_movement), "values": {}},
    )


@router.get("/form/base-sides")
def form_base_sides(request: Request, store: StoreDep, move_type: str = ""):
    return templates.TemplateResponse(
        request,
        "_base_sides.html",
        {"sides": base_sides(store.load(), move_type), "values": {}},
    )


@router.post("/movements")
def create_movement(
    request: Request,
    store: StoreDep,
    move_date: Annotated[str, Form()],
    base_movement: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    move_type: Annotated[str, Form()] = "",
    acc_debit: Annotated[str, Form()] = "",
    acc_credit: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
):
    values = {
        "move_date": move_date, "move_type": move_type, "base_movement": base_movement,
        "acc_debit": acc_debit, "acc_credit": acc_credit,
        "amount": amount, "comment": comment,
    }
    try:
        store.add(move_date, base_movement, acc_debit, acc_credit, amount, comment)
    except ValueError as e:
        return render_form(request, store.load(), values, error=str(e))
    # Keep date, type and base movement to speed up entering similar movements.
    keep = {"move_date": move_date, "move_type": move_type, "base_movement": base_movement}
    value = float(amount.replace(",", ""))
    response = render_form(
        request, store.load(), keep,
        success=f"Added {base_movement}: {money(value, store.settings.currency)}",
    )
    response.headers["HX-Trigger"] = "ledger-changed"
    return response


@router.post("/movements/{index}/delete", response_class=Response)
def delete_movement(store: StoreDep, index: int, fingerprint: Annotated[str, Form()]):
    try:
        store.delete(index, fingerprint)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    return Response(status_code=200, headers={"HX-Trigger": "ledger-changed"})


def render_setup(request: Request, store: LedgerStore, values: dict, **extra):
    return templates.TemplateResponse(
        request,
        "_setup.html",
        {**setup_context(store.load(), values), "settings": store.settings, **extra},
    )


def render_accounts(request: Request, store: LedgerStore, values: dict, **extra):
    snapshot = store.load()
    month = remembered_month(request, None, months_of(snapshot))
    return templates.TemplateResponse(
        request, "_accounts.html", {**build_accounts(snapshot, month, values), **extra}
    )


@router.post("/accounts")
def create_account(
    request: Request,
    store: StoreDep,
    name: Annotated[str, Form()],
    acc_type: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
):
    try:
        store.add_account(name, acc_type, description)
    except ValueError as e:
        values = {"acc_type": acc_type, "acc_name": name, "acc_description": description}
        return render_accounts(request, store, values, account_error=str(e))
    return render_accounts(
        request, store, {"acc_type": acc_type}, account_success=f"Added account {name.strip()}"
    )


@router.post("/setup/result-types")
def create_result_type(
    request: Request,
    store: StoreDep,
    name: Annotated[str, Form()],
    long_name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
):
    try:
        store.add_result_type(name, long_name, description)
    except ValueError as e:
        values = {"rt_name": name, "rt_long_name": long_name, "rt_description": description}
        return render_setup(request, store, values, result_type_error=str(e))
    return render_setup(
        request, store, {}, result_type_success=f"Added result type {name.strip()}"
    )


@router.post("/setup/base-movements")
def create_base_movement(
    request: Request,
    store: StoreDep,
    name: Annotated[str, Form()],
    move_type: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    debit: Annotated[str, Form()] = "",
    credit: Annotated[str, Form()] = "",
    result_type: Annotated[str, Form()] = "",
):
    try:
        store.add_base_movement(name, move_type, description, debit, credit, result_type)
    except ValueError as e:
        values = {
            "move_type": move_type, "base_name": name, "base_description": description,
            "debit": debit, "credit": credit, "result_type": result_type,
        }
        return render_setup(request, store, values, base_error=str(e))
    return render_setup(
        request, store, {"move_type": move_type, "result_type": result_type},
        base_success=f"Added base movement {name.strip()}",
    )
