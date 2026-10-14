"""forms.py: HTMX endpoints behind the movement form and ledger view."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from pledger.web.dependencies import StoreDep
from pledger.web.store import StaleRowError
from pledger.web.templating import money, render_form, templates
from pledger.web.views import account_fields

router = APIRouter(tags=["forms"], default_response_class=HTMLResponse)


@router.get("/form/accounts")
def form_accounts(request: Request, store: StoreDep, base_movement: str = ""):
    snapshot = store.load()
    return templates.TemplateResponse(
        request,
        "_account_fields.html",
        {"fields": account_fields(snapshot, base_movement), "values": {}},
    )


@router.post("/movements")
def create_movement(
    request: Request,
    store: StoreDep,
    move_date: Annotated[str, Form()],
    base_movement: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    acc_debit: Annotated[str, Form()] = "",
    acc_credit: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
):
    values = {
        "move_date": move_date, "base_movement": base_movement,
        "acc_debit": acc_debit, "acc_credit": acc_credit,
        "amount": amount, "comment": comment,
    }
    try:
        store.add(move_date, base_movement, acc_debit, acc_credit, amount, comment)
    except ValueError as e:
        return render_form(request, store.load(), values, error=str(e))
    # Keep date and base movement to speed up entering similar movements.
    keep = {"move_date": move_date, "base_movement": base_movement}
    response = render_form(
        request, store.load(), keep,
        success=f"Added {base_movement}: {money(float(amount.replace(',', '')))}",
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
