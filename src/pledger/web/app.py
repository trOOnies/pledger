"""app.py"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from pledger.enums import AccountType, MovementType
from pledger.web.store import (
    LedgerDataError,
    LedgerStore,
    Settings,
    Snapshot,
    StaleRowError,
    row_fingerprint,
)

HERE = Path(__file__).parent
ACCOUNT_TYPE_ORDER = list(AccountType)


class MovementIn(BaseModel):
    move_date: str
    base_movement: str
    acc_debit: str
    acc_credit: str
    amount: float
    comment: str = ""


def money(value: float) -> str:
    return f"{value:,.2f}"


def build_view(snapshot: Snapshot, month: str = "", account: str = "") -> dict:
    """Filtered movements, totals and balances for the ledger view."""
    rows = snapshot.rows
    months = sorted({d[:7] for d in rows.move_date}, reverse=True)

    mask = rows.move_date.str.startswith(month) if month else rows.move_date.notna()
    if account:
        mask &= (rows.acc_debit == account) | (rows.acc_credit == account)
    filtered = rows[mask]
    ledger = snapshot.ledger(filtered)

    # Balances are cumulative: as of the end of the selected month.
    upto = rows[rows.move_date.str[:7] <= month] if month else rows
    balances = snapshot.ledger(upto).balances()

    movements = [
        {
            "index": int(ix),
            "fingerprint": row_fingerprint(row),
            "date": move.move_date,
            "base": move.move_base.name,
            "type": move.move_type.value,
            "debit": move.acc_debit.name,
            "credit": move.acc_credit.name,
            "amount": move.amount,
            "comment": row.get("comment", ""),
        }
        for (ix, row), move in zip(filtered.iterrows(), ledger.movements)
    ]
    movements.reverse()  # newest first

    accounts_by_type = {
        t.value: [
            {"name": a.name, "description": a.description, "balance": balances.get(a.name, 0.0)}
            for a in snapshot.accounts.values()
            if a.acc_type == t
        ]
        for t in ACCOUNT_TYPE_ORDER
    }
    return {
        "months": months,
        "month": month,
        "account": account,
        "accounts": list(snapshot.accounts),
        "movements": movements,
        "gains": ledger.total_gains,
        "losses": ledger.total_losses,
        "profit": ledger.total_profits,
        "accounts_by_type": accounts_by_type,
    }


def account_fields(snapshot: Snapshot, base_name: str) -> dict:
    """Which debit/credit accounts are fixed or selectable for a base movement."""
    base = snapshot.base_movements.get(base_name)
    free = [a.name for a in snapshot.free_accounts()]
    if base is None:
        return {"debit_fixed": None, "credit_fixed": None, "free": free, "type": ""}
    return {
        "debit_fixed": base.acc_debit.name if base.acc_debit else None,
        "credit_fixed": base.acc_credit.name if base.acc_credit else None,
        "free": free,
        "type": base.move_type.value,
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    store = LedgerStore(settings or Settings.from_env())
    app = FastAPI(title="pledger")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["money"] = money

    @app.exception_handler(LedgerDataError)
    async def ledger_data_error(request: Request, exc: LedgerDataError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": str(exc)}, status_code=500)
        return templates.TemplateResponse(
            request, "error.html", {"error": str(exc), "settings": store.settings},
            status_code=500,
        )

    def is_partial(request: Request) -> bool:
        return (
            request.headers.get("HX-Request") == "true"
            and request.headers.get("HX-History-Restore-Request") != "true"
        )

    def render_form(request: Request, snapshot: Snapshot, values: dict, **extra):
        base_names = sorted(snapshot.base_movements)
        values.setdefault("base_movement", base_names[0] if base_names else "")
        values.setdefault("move_date", dt.date.today().isoformat())
        return templates.TemplateResponse(
            request,
            "_form.html",
            {
                "bases_by_type": {
                    t.value: [b for b in base_names if snapshot.base_movements[b].move_type == t]
                    for t in MovementType
                },
                "values": values,
                "fields": account_fields(snapshot, values["base_movement"]),
                **extra,
            },
        )

    # ---------------------------------------------------------------- HTML

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, month: str = "", account: str = ""):
        snapshot = store.load()
        view = build_view(snapshot, month, account)
        if is_partial(request):
            return templates.TemplateResponse(request, "_view.html", view)
        form = render_form(request, snapshot, {}).body.decode()
        return templates.TemplateResponse(
            request, "index.html", {**view, "form": form}
        )

    @app.get("/form/accounts", response_class=HTMLResponse)
    def form_accounts(request: Request, base_movement: str = ""):
        snapshot = store.load()
        return templates.TemplateResponse(
            request,
            "_account_fields.html",
            {"fields": account_fields(snapshot, base_movement), "values": {}},
        )

    @app.post("/movements", response_class=HTMLResponse)
    def create_movement(
        request: Request,
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

    @app.post("/movements/{index}/delete")
    def delete_movement(index: int, fingerprint: Annotated[str, Form()]):
        try:
            store.delete(index, fingerprint)
        except StaleRowError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        return Response(status_code=200, headers={"HX-Trigger": "ledger-changed"})

    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request):
        snapshot = store.load()
        return templates.TemplateResponse(
            request,
            "setup.html",
            {
                "accounts_by_type": {
                    t.value: [a for a in snapshot.accounts.values() if a.acc_type == t]
                    for t in ACCOUNT_TYPE_ORDER
                },
                "bases_by_type": {
                    t.value: [b for b in snapshot.base_movements.values() if b.move_type == t]
                    for t in MovementType
                },
                "settings": store.settings,
            },
        )

    # ----------------------------------------------------------------- API

    @app.get("/api/accounts")
    def api_accounts():
        return [
            {"name": a.name, "description": a.description, "type": a.acc_type.value}
            for a in store.load().accounts.values()
        ]

    @app.get("/api/base-movements")
    def api_base_movements():
        return [
            {
                "name": b.name,
                "description": b.description,
                "type": b.move_type.value,
                "debit": b.acc_debit.name if b.acc_debit else None,
                "credit": b.acc_credit.name if b.acc_credit else None,
            }
            for b in store.load().base_movements.values()
        ]

    @app.get("/api/movements")
    def api_movements(month: str = "", account: str = ""):
        return build_view(store.load(), month, account)["movements"]

    @app.get("/api/summary")
    def api_summary(month: str = "", account: str = ""):
        view = build_view(store.load(), month, account)
        return {
            "gains": view["gains"],
            "losses": view["losses"],
            "profit": view["profit"],
            "balances": {
                a["name"]: a["balance"]
                for accs in view["accounts_by_type"].values()
                for a in accs
            },
        }

    @app.post("/api/movements", status_code=201)
    def api_create_movement(movement: MovementIn):
        try:
            store.add(**movement.model_dump())
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        return {"ok": True}

    @app.delete("/api/movements/{index}")
    def api_delete_movement(index: int, fingerprint: Annotated[str | None, Query()] = None):
        try:
            store.delete(index, fingerprint)
        except StaleRowError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        return {"ok": True}

    return app


app = create_app()
