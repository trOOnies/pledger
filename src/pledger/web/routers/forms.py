"""forms.py: HTMX endpoints behind the movement and setup forms."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from pledger.models import INTERMEDIARY, DebtAccount
from pledger.web.dependencies import StoreDep
from pledger.web.store import (
    CARD_SURPLUS,
    OTHER_AMOUNT,
    LedgerStore,
    Snapshot,
    StaleRowError,
    foreign_card,
    foreign_result_account,
    linked_conversion,
)
from pledger.web.store.common import _amount_of, _value_on
from pledger.web.templating import (
    money,
    render_card_payment_form,
    render_conversion_form,
    render_debt_form,
    render_form,
    render_invest_form,
    render_new_investment_form,
    render_prices_form,
    render_service_form,
    templates,
)
from pledger.web.views import (
    EDITABLE_ACCOUNT_TYPES,
    account_editing,
    account_fields,
    account_form,
    account_values,
    audit_entries,
    audit_form,
    audit_history,
    audit_results,
    base_fields,
    base_sides,
    build_conversions,
    build_invest_movements,
    build_work,
    conversion_fields,
    conversion_filter,
    debt_difference,
    debt_form_fields,
    invest_fields,
    invest_filter,
    setup_context,
    work_form,
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
        store.add_ledger_movement(move_date, base_movement, acc_debit, acc_credit, amount, comment)
    except ValueError as e:
        return render_form(request, store.load(), values, error=str(e))
    # Keep date, type and base movement to speed up entering similar movements.
    keep = {"move_date": move_date, "move_type": move_type, "base_movement": base_movement}
    value = float(amount.replace(",", ""))
    account = foreign_result_account(store.load(), base_movement, acc_debit, acc_credit)
    shown = f"{value:,.2f} {account.currency.name}" if account else money(value, store.settings.currency)
    response = render_form(
        request, store.load(), keep,
        success=f"Added {base_movement}: {shown}"
        + (" (dated in the future)" if move_date > dt.date.today().isoformat() else ""),
    )
    response.headers["HX-Trigger"] = "ledger-changed"
    return response


@router.get("/form/movement")
def movement_form(request: Request, store: StoreDep):
    """A new movement form, e.g. to cancel editing one."""
    return render_form(request, store.load(), {})


# The rows of the movements each CSV gives the ledger, in its order: they have the ids.
LEDGER_ROWS = {"rows": "rows", "investments": "investment_rows", "interest": "interest_rows", "conversions": "conversion_rows"}


def editing_of(store: LedgerStore, field: str, index: int, fingerprint: str, url: str = "") -> tuple[dict, Snapshot]:
    """
    What a form needs to edit a row of the CSV of a Snapshot `field` (see
    LedgerStore.current_row), checking it is the expected one, and the snapshot.
    """
    snapshot = store.load()
    try:
        store.current_row(snapshot, field, index, fingerprint)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    ix = getattr(snapshot, LEDGER_ROWS[field]).index[index]
    return {"index": index, "fingerprint": fingerprint, "id": snapshot.ids.get(ix), "url": url}, snapshot


def updated(editing: dict, what: str, move_date: str) -> str:
    """The success message of an edit."""
    future = " (dated in the future)" if move_date > dt.date.today().isoformat() else ""
    return f"Updated {editing['id'] + ' ' if editing['id'] else ''}{what}{future}"


def copy_values(snapshot: Snapshot, index: int, own: str | None) -> dict:
    """
    The values of the movement form to add a movement like a row of the ledger CSV: as
    add_ledger_movement takes them, so with the account and amount of its linked conversion, if
    any, and the amount of a gain or loss of an account in another currency in that currency.
    """
    row = snapshot.rows.iloc[index]
    values = ledger_values(row)
    conversion = linked_conversion(snapshot, own)
    if conversion is not None:
        names = (conversion["acc_debit"], conversion["acc_credit"])
        account = next(n for n in names if not snapshot.accounts[n].currency.is_base_currency)
        side = "acc_debit" if values["acc_debit"] == INTERMEDIARY else "acc_credit"
        return {**values, side: account, "amount": conversion[OTHER_AMOUNT]}
    account = foreign_result_account(snapshot, row["base_movement"], row["acc_debit"], row["acc_credit"])
    if account is not None:
        rate = _value_on(account.currency, row["move_date"], "to copy the movement")
        values["amount"] = f"{_amount_of(row['amount']) / rate:,.2f}"
    return values


def ledger_values(row) -> dict:
    """The values of the movement form of a ledger CSV row."""
    return {
        "move_date": row["move_date"], "base_movement": row["base_movement"],
        "acc_debit": row["acc_debit"], "acc_credit": row["acc_credit"],
        "amount": row["amount"], "comment": row.get("comment", ""),
    }


@router.get("/movements/{index}/edit")
def edit_movement_form(request: Request, store: StoreDep, index: int, fingerprint: str):
    """The movement form filled with a row of the ledger CSV, to edit it."""
    editing, snapshot = editing_of(store, "rows", index, fingerprint)
    return render_form(request, snapshot, ledger_values(snapshot.rows.iloc[index]), editing=editing)


@router.get("/movements/{index}/copy")
def copy_movement_form(request: Request, store: StoreDep, index: int, fingerprint: str):
    """A new movement form filled with a row of the ledger CSV, which is left as it is."""
    origin, snapshot = editing_of(store, "rows", index, fingerprint)
    return render_form(request, snapshot, copy_values(snapshot, index, origin["id"]), copied=origin["id"] or "another movement")


@router.post("/movements/{index}/edit")
def edit_movement(
    request: Request,
    store: StoreDep,
    index: int,
    fingerprint: Annotated[str, Form()],
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
    editing, _ = editing_of(store, "rows", index, fingerprint)
    try:
        store.edit(index, fingerprint, move_date, base_movement, acc_debit, acc_credit, amount, comment)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        return render_form(request, store.load(), values, error=str(e), editing=editing)
    value = money(float(amount.replace(",", "")), store.settings.currency)
    response = render_form(request, store.load(), {}, success=updated(editing, f"{base_movement}: {value}", move_date))
    response.headers["HX-Trigger"] = "ledger-changed"
    return response


@router.post("/movements/{index}/delete", response_class=Response)
def delete_movement(store: StoreDep, index: int, fingerprint: Annotated[str, Form()]):
    """Delete a movement of the Ledger tab, a balance correction (see LedgerStore.correct_balance) or a Difference."""
    snapshot = store.load()
    rows = snapshot.rows
    base = rows.iloc[index]["base_movement"] if 0 <= index < len(rows) else None
    try:
        store.delete(
            index, fingerprint, correction=base in snapshot.correction_base_movements(),
            difference=base in snapshot.difference_base_movements(),
        )
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return Response(status_code=200, headers={"HX-Trigger": "ledger-changed"})


@router.get("/form/invest-bases")
def form_invest_bases(request: Request, store: StoreDep, move_type: str = ""):
    return templates.TemplateResponse(
        request, "_invest_bases.html", {**invest_fields(store.load(), move_type=move_type), "values": {}, "oob": True}
    )


@router.get("/form/invest-fields")
def form_invest_fields(request: Request, store: StoreDep, base_movement: str = ""):
    return templates.TemplateResponse(
        request, "_invest_fields.html", {**invest_fields(store.load(), base_movement), "values": {}, "oob": True}
    )


@router.get("/investments/movements")
def investment_movements(request: Request, store: StoreDep, account: str = "", investment: str = "", base: str = ""):
    """
    The investment movements, of the investment account and investment picked in the filter
    if any (see views.invest_filter), with the filter itself, out of band.
    """
    return templates.TemplateResponse(
        request, "_invest_movements.html",
        {**invest_filter(build_invest_movements(store.load()), account, investment, base), "oob": True},
    )


@router.post("/investments/movements")
def create_investment_movement(
    request: Request,
    store: StoreDep,
    move_date: Annotated[str, Form()],
    base_movement: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    move_type: Annotated[str, Form()] = "",
    acc_debit: Annotated[str, Form()] = "",
    acc_credit: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
    investment: Annotated[str, Form()] = "",
    quantity: Annotated[str, Form()] = "",
    investment_account: Annotated[str, Form()] = "",  # only narrows down the investments of the form
    currency: Annotated[str, Form()] = "",
    fees: Annotated[str, Form()] = "",
):
    values = {
        "move_date": move_date, "move_type": move_type, "base_movement": base_movement,
        "acc_debit": acc_debit, "acc_credit": acc_credit,
        "amount": amount, "comment": comment,
        "investment": investment, "quantity": quantity, "investment_account": investment_account,
        "currency": currency, "fees": fees,
    }
    try:
        store.add_investment_movement(
            move_date, base_movement, acc_debit, acc_credit, amount, comment,
            investment=investment, quantity=quantity, currency=currency, fees=fees,
        )
    except ValueError as e:
        return render_invest_form(request, store.load(), values, error=str(e))
    # Keep date, type and base movement to speed up entering similar movements.
    keep = {"move_date": move_date, "move_type": move_type, "base_movement": base_movement, "currency": currency}
    value = float(amount.replace(",", ""))
    of = f" of {investment.strip()}" if investment.strip() else ""
    base_name = store.load().base_currency.name
    shown = money(value, store.settings.currency) if currency in ("", base_name) else f"{value:,.2f} {currency}"
    response = render_invest_form(
        request, store.load(), keep,
        success=f"Added {base_movement}{of}: {shown}"
        + (" (dated in the future)" if move_date > dt.date.today().isoformat() else ""),
    )
    response.headers["HX-Trigger"] = "investments-changed"
    return response


@router.post("/investments/new")
def create_investment(
    request: Request,
    store: StoreDep,
    account: Annotated[str, Form()],
    name: Annotated[str, Form()],
    investment_type: Annotated[str, Form()],
    quantity: Annotated[str, Form()] = "",
    price: Annotated[str, Form()] = "",
):
    values = {"account": account, "name": name, "investment_type": investment_type, "quantity": quantity, "price": price}
    try:
        store.add_investment(account, name, investment_type, quantity, price)
    except ValueError as e:
        return render_new_investment_form(request, store.load(), values, error=str(e))
    # Keep the account and type to speed up adding similar investments.
    response = render_new_investment_form(
        request, store.load(), {"account": account, "investment_type": investment_type},
        success=f"Added {name.strip()} to {account}",
    )
    response.headers["HX-Trigger"] = "investment-added"
    return response


@router.get("/services/form")
def services_month_form(request: Request, store: StoreDep, month: str = ""):
    """The services month form, filled with a month's values to edit it."""
    return render_service_form(request, store.load(), month.strip())


@router.post("/services/months")
async def save_service_month(request: Request, store: StoreDep, month: Annotated[str, Form()] = ""):
    # One "service:<service>" field per service of the services CSV
    values = {
        k.removeprefix("service:"): str(v) for k, v in (await request.form()).items() if k.startswith("service:")
    }
    try:
        store.save_service_month(month, values)
    except ValueError as e:
        return render_service_form(request, store.load(), month.strip(), values, error=str(e))
    response = render_service_form(request, store.load(), month, success=f"Saved {month}")
    response.headers["HX-Trigger"] = "services-changed"
    return response


def entered_prices(account: list[str] | None, investment: list[str] | None, price: list[str] | None) -> dict:
    """What the prices form has typed in, row by row, by account and investment."""
    rows = zip(account or [], investment or [], price or [])
    return {(a, name): value for a, name, value in rows}


@router.get("/form/prices")
def prices_form(
    request: Request,
    store: StoreDep,
    date: str = "",
    account: Annotated[list[str] | None, Query()] = None,
    investment: Annotated[list[str] | None, Query()] = None,
    price: Annotated[list[str] | None, Query()] = None,
):
    """The prices form, up to date with the investments, keeping what was typed in."""
    return render_prices_form(request, store.load(), date, entered_prices(account, investment, price))


@router.post("/investments/prices")
def update_prices(
    request: Request,
    store: StoreDep,
    date: Annotated[str, Form()],
    account: Annotated[list[str] | None, Form()] = None,
    investment: Annotated[list[str] | None, Form()] = None,
    price: Annotated[list[str] | None, Form()] = None,
):
    entered = entered_prices(account, investment, price)
    try:
        added, unchanged = store.add_prices(date, [(*key, value) for key, value in entered.items()])
    except ValueError as e:
        return render_prices_form(request, store.load(), date, entered, error=str(e))
    # Keep the date, for other investments priced on it.
    skipped = f"; {unchanged} unchanged left out" if unchanged else ""
    if not added:
        message = "No price changed: nothing to add" if unchanged else "Fill in a new price to add it"
        return render_prices_form(request, store.load(), date, entered, error=message)
    response = render_prices_form(
        request, store.load(), date, success=f"Added {added} price{'s' if added > 1 else ''} on {date}{skipped}",
    )
    response.headers["HX-Trigger"] = "prices-changed"
    return response


@router.post("/investments/movements/{index}/delete", response_class=Response)
def delete_investment_movement(store: StoreDep, index: int, fingerprint: Annotated[str, Form()]):
    try:
        store.delete_investment(index, fingerprint)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return Response(status_code=200, headers={"HX-Trigger": "investments-changed"})


@router.post("/investments/interest/{index}/delete", response_class=Response)
def delete_interest_movement(store: StoreDep, index: int, fingerprint: Annotated[str, Form()]):
    try:
        store.delete_interest(index, fingerprint)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return Response(status_code=200, headers={"HX-Trigger": "investments-changed"})


@router.get("/form/conversion-fields")
def form_conversion_fields(
    request: Request, store: StoreDep, sold: str = "", bought: str = "", changed: str = "",
    acc_from: str = "", acc_to: str = "", amount_sold: str = "", amount_bought: str = "",
):
    snapshot = store.load()
    values = {"acc_from": acc_from, "acc_to": acc_to, "amount_sold": amount_sold, "amount_bought": amount_bought}
    return templates.TemplateResponse(
        request,
        "_conversion_fields.html",
        {
            **conversion_fields(snapshot, sold, bought, changed),
            "currency": store.settings.currency,
            "base_currency": snapshot.base_currency.name,
            "values": values,
        },
    )


@router.get("/conversions")
def conversions(request: Request, store: StoreDep, currency: str = "", account: str = "", initial: bool = False):
    """
    The conversions, of the currency and account picked in the filter if any, and the
    initial movements if asked (see views.conversion_filter), with the filter itself, out of band.
    """
    return templates.TemplateResponse(
        request, "_conversions.html", {**conversion_filter(store.load(), currency, account, initial), "oob": True}
    )


@router.post("/conversions")
def create_conversion(
    request: Request,
    store: StoreDep,
    move_date: Annotated[str, Form()],
    sold: Annotated[str, Form()],
    bought: Annotated[str, Form()],
    amount_sold: Annotated[str, Form()],
    amount_bought: Annotated[str, Form()],
    acc_from: Annotated[str, Form()] = "",
    acc_to: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
):
    values = {
        "move_date": move_date, "sold": sold, "bought": bought, "acc_from": acc_from, "acc_to": acc_to,
        "amount_sold": amount_sold, "amount_bought": amount_bought, "comment": comment,
    }
    try:
        base_movement = store.add_exchange(
            move_date, sold, bought, acc_from, acc_to, amount_sold, amount_bought, comment
        )
    except ValueError as e:
        return render_conversion_form(request, store.load(), values, error=str(e))
    # Keep date, currencies and accounts to speed up entering similar conversions.
    keep = {
        "move_date": move_date, "sold": sold, "bought": bought, "acc_from": acc_from, "acc_to": acc_to,
    }
    value = float(amount_sold.replace(",", ""))
    response = render_conversion_form(
        request, store.load(), keep,
        success=f"Added {base_movement}: {value:,.2f} {sold} sold for {float(amount_bought.replace(',', '')):,.2f} {bought}"
        + (" (dated in the future)" if move_date > dt.date.today().isoformat() else ""),
    )
    response.headers["HX-Trigger"] = "conversions-changed"
    return response


@router.post("/conversions/{index}/delete", response_class=Response)
def delete_conversion(store: StoreDep, index: int, fingerprint: Annotated[str, Form()]):
    try:
        store.delete_conversion(index, fingerprint)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return Response(status_code=200, headers={"HX-Trigger": "conversions-changed"})


@router.post("/debt/movements")
def create_debt_movement(
    request: Request,
    store: StoreDep,
    move_date: Annotated[str, Form()],
    base_movement: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    who: Annotated[str, Form()] = "",
    acc_debit: Annotated[str, Form()] = "",
    acc_credit: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
    remaining_as_difference: Annotated[bool, Form()] = False,
):
    values = {
        "move_date": move_date, "base_movement": base_movement, "acc_debit": acc_debit,
        "acc_credit": acc_credit, "amount": amount, "who": who, "comment": comment,
        "remaining_as_difference": remaining_as_difference,
    }
    try:
        difference = store.add_debt_movement(
            move_date, base_movement, acc_debit, acc_credit, amount, who, comment, remaining_as_difference,
        )
    except ValueError as e:
        return render_debt_form(request, store.load(), values, error=str(e))
    # Keep date and base movement to speed up entering similar movements.
    keep = {"move_date": move_date, "base_movement": base_movement}
    response = render_debt_form(
        request, store.load(), keep,
        success=f"Added {base_movement} ({who.strip()}): {money(float(amount.replace(',', '')), store.settings.currency)}"
        + (f" and {difference[0]}: {money(difference[1], store.settings.currency)}" if difference else "")
        + (" (dated in the future)" if move_date > dt.date.today().isoformat() else ""),
    )
    response.headers["HX-Trigger"] = "debt-changed"
    return response


@router.post("/credit-cards/payments")
def create_card_payment(
    request: Request,
    store: StoreDep,
    move_date: Annotated[str, Form()],
    base_movement: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    acc_debit: Annotated[str, Form()] = "",
    acc_credit: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
    amount_card: Annotated[str, Form()] = "",
):
    values = {
        "move_date": move_date, "base_movement": base_movement, "acc_debit": acc_debit,
        "acc_credit": acc_credit, "amount": amount, "comment": comment, "amount_card": amount_card,
    }
    try:
        store.add_card_payment(move_date, base_movement, acc_debit, acc_credit, amount, comment, amount_card)
    except ValueError as e:
        return render_card_payment_form(request, store.load(), values, error=str(e))
    # Keep date and base movement to speed up entering similar payments.
    keep = {"move_date": move_date, "base_movement": base_movement}
    response = render_card_payment_form(
        request, store.load(), keep,
        success=(f"Surplus of {acc_credit}" if base_movement == CARD_SURPLUS else f"Paid {acc_debit}")
        + f": {card_amount_text(store, acc_debit, acc_credit, amount)}"
        + (" (dated in the future)" if move_date > dt.date.today().isoformat() else ""),
    )
    response.headers["HX-Trigger"] = "credit-cards-changed"
    return response


def card_amount_text(store: LedgerStore, acc_debit: str, acc_credit: str, amount: str) -> str:
    """A card payment in the success message: in the currency of the other account, for a card in another one."""
    snapshot, value = store.load(), float(amount.replace(",", ""))
    card = foreign_card(snapshot, acc_debit, acc_credit)
    if card is None:
        return money(value, store.settings.currency)
    return f"{value:,.2f} {snapshot.accounts[acc_credit if card.name == acc_debit else acc_debit].currency.name}"


def render_setup(request: Request, store: LedgerStore, values: dict, **extra):
    return templates.TemplateResponse(
        request,
        "_setup.html",
        {**setup_context(store.load(), values), "settings": store.settings, **extra},
    )


def render_account_form(
    request: Request, store: LedgerStore, values: dict, editing: dict | None = None,
    changed: bool = False, **extra,
):
    """
    The account form alone (see views.account_form); `changed` tells the rest of the
    Accounts tab to reload.
    """
    response = templates.TemplateResponse(
        request, "_account_form.html", {**account_form(store.load(), values, editing), **extra}
    )
    if changed:
        response.headers["HX-Trigger"] = "accounts-changed"
    return response


@router.get("/form/account")
def new_account_form(request: Request, store: StoreDep):
    """A new account form, e.g. to cancel editing an account."""
    return render_account_form(request, store, {})


@router.post("/accounts")
def create_account(
    request: Request,
    store: StoreDep,
    name: Annotated[str, Form()],
    acc_type: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    # Required, but checked by the store so the form shows a readable error
    initial_amount: Annotated[str, Form()] = "",
    currency: Annotated[str, Form()] = "",
    is_liquid: Annotated[bool, Form()] = False,
    initial_date: Annotated[str, Form()] = "",
    check: Annotated[str, Form()] = "",  # one AccountCheck value, or none
):
    try:
        store.add_account(
            name, acc_type, description, initial_amount, currency, is_liquid, initial_date,
            [check] if check else [],
        )
    except ValueError as e:
        values = {
            "acc_type": acc_type, "acc_name": name, "acc_description": description,
            "acc_currency": currency, "acc_is_liquid": is_liquid,
            "acc_initial_amount": initial_amount, "acc_initial_date": initial_date,
            "acc_check": check,
        }
        return render_account_form(request, store, values, account_error=str(e))
    return render_account_form(
        request, store, {"acc_type": acc_type, "acc_currency": currency},
        changed=True, account_success=f"Added account {name.strip()}",
    )


def editable_account(store: LedgerStore, name: str):
    """An account that can be edited in the Accounts tab (see views.EDITABLE_ACCOUNT_TYPES)."""
    account = store.load().accounts.get(name)
    if account is None or account.acc_type not in EDITABLE_ACCOUNT_TYPES:
        raise HTTPException(status_code=404, detail=f"No asset or liability account {name!r}")
    return account


@router.get("/accounts/edit")
def edit_account_form(request: Request, store: StoreDep, name: str):
    """The account form filled with an account, to edit it."""
    account = editable_account(store, name)
    return render_account_form(request, store, account_values(account), editing=account_editing(account))


@router.post("/accounts/edit")
def edit_account(
    request: Request,
    store: StoreDep,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    initial_amount: Annotated[str, Form()] = "",
    is_liquid: Annotated[bool, Form()] = False,
    initial_date: Annotated[str, Form()] = "",
    check: Annotated[str, Form()] = "",  # one AccountCheck value, or none
):
    account = editable_account(store, name)
    try:
        store.edit_account(name, description, initial_amount, is_liquid, initial_date, [check] if check else [])
    except ValueError as e:
        values = {
            "acc_type": account.acc_type.value, "acc_name": name, "acc_description": description,
            "acc_currency": account.currency.name, "acc_is_liquid": is_liquid,
            "acc_initial_amount": initial_amount, "acc_initial_date": initial_date,
            "acc_check": check,
        }
        return render_account_form(request, store, values, editing=account_editing(account), account_error=str(e))
    return render_account_form(request, store, {}, changed=True, account_success=f"Updated account {name}")


def audit_values(form) -> dict[str, str]:
    """The balances filled in, by account, from a "balance:<account>" field per account."""
    return {k.removeprefix("balance:"): str(v) for k, v in form.items() if k.startswith("balance:")}


def render_audit(request: Request, store: LedgerStore, values: dict[str, str], save: bool = False, **extra):
    """
    The results of an audit, saved to the history with `save`, or the form again with what
    is wrong with it (see views.audit_results).
    """
    snapshot = store.load()
    try:
        context = audit_results(snapshot, values)
        if save:
            context["audit_id"] = store.add_audit(audit_entries(context["results"]))
    except ValueError as e:
        context = {**audit_form(snapshot, values), "audit_error": str(e)}
    context["history"] = audit_history(snapshot, store.load_audits())
    return templates.TemplateResponse(request, "_audit.html", {**context, **extra})


@router.post("/audit")
async def audit(request: Request, store: StoreDep):
    """Confirm the balances filled in: only now are they shown next to the actual ones, and saved as an audit."""
    return render_audit(request, store, audit_values(await request.form()), save=True)


@router.post("/audit/correct")
async def correct_balance(request: Request, store: StoreDep):
    """
    Correct an account to its balance filled in (see LedgerStore.correct_balance), marking
    it in its audit, keeping the results.
    """
    form = await request.form()
    name, values, audit_id = str(form.get("account", "")), audit_values(form), str(form.get("audit", ""))
    try:
        corrected = store.correct_balance(name, values.get(name, ""))
    except ValueError as e:
        return render_audit(request, store, values, audit_failure=str(e), audit_id=audit_id)
    if corrected is None:
        return render_audit(request, store, values, audit_success=f"The balance of {name} was already right", audit_id=audit_id)
    base_movement, amount = corrected
    store.mark_correction(audit_id, name, base_movement, amount)
    return render_audit(
        request, store, values, audit_id=audit_id,
        audit_success=f"Corrected {name}: {base_movement} of {money(amount, store.settings.currency)}",
    )


def render_work(request: Request, store: LedgerStore, form: dict | None = None, **extra):
    return templates.TemplateResponse(
        request, "_work.html", {**build_work(store.load_work(), form, store.load_cpi()), **extra}
    )


@router.get("/work/form")
def work_month_form(request: Request, store: StoreDep, month: str = ""):
    """The work month form, filled with a month's values to edit it."""
    return templates.TemplateResponse(
        request, "_work_form.html", {"form": work_form(store.load_work(), month.strip())}
    )


@router.post("/work/months")
async def save_work_month(
    request: Request,
    store: StoreDep,
    month: Annotated[str, Form()] = "",
    company: Annotated[str, Form()] = "",
    position: Annotated[str, Form()] = "",
    gross: Annotated[str, Form()] = "",
    payment_date: Annotated[str, Form()] = "",
):
    # One "deduction:<base movement>" field per deduction of the salary
    deductions = {
        k.removeprefix("deduction:"): str(v) for k, v in (await request.form()).items() if k.startswith("deduction:")
    }
    try:
        created = store.save_month(month, company, position, gross, deductions, payment_date)
    except ValueError as e:
        values = {
            "company": company, "position": position, "gross": gross, "deductions": deductions,
            "payment_date": payment_date,
        }
        work = store.load_work()
        form = {**work_form(work, month.strip(), values), "error": str(e)}
        return render_work(request, store, form)
    work = store.load_work()
    what = f"{position.strip()} at {company.strip()}" if company.strip() else "no job"
    form = {
        **work_form(work),
        "success": f"{'Added' if created else 'Updated'} {month.strip()} ({what})",
    }
    return render_work(request, store, form)


@router.post("/work/months/{index}/delete")
def delete_work_month(
    request: Request, store: StoreDep, index: int, fingerprint: Annotated[str, Form()]
):
    try:
        store.delete_month(index, fingerprint)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return render_work(request, store)


@router.post("/results/result-types")
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


@router.post("/results/base-movements")
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


INVEST_SOURCES = {"movements": "investments", "interest": "interest"}  # in the URLs, the CSV of the movement


@router.get("/form/invest-movement")
def invest_movement_form(request: Request, store: StoreDep):
    """A new investment movement form, e.g. to cancel editing one."""
    return render_invest_form(request, store.load(), {})


@router.get("/investments/{source}/{index}/edit")
def edit_investment_movement_form(
    request: Request, store: StoreDep, source: Literal["movements", "interest"], index: int, fingerprint: str
):
    """The investment movement form filled with a row of the investments or interest CSV, to edit it."""
    field = INVEST_SOURCES[source]
    editing, snapshot = editing_of(store, field, index, fingerprint, f"/investments/{source}/{index}/edit")
    m = next(m for m in build_invest_movements(snapshot) if m["source"] == field and m["index"] == index)
    values = {
        "move_date": m["date"], "move_type": m["type"], "base_movement": m["base"],
        "acc_debit": m["debit"], "acc_credit": m["credit"], "amount": f"{m['amount']:,.2f}",
        "comment": m["comment"], "investment": m["investment"], "quantity": m["quantity"],
        "investment_account": m["account"] or "",
    }
    return render_invest_form(request, snapshot, values, editing=editing)


@router.post("/investments/{source}/{index}/edit")
def edit_investment_movement(
    request: Request,
    store: StoreDep,
    source: Literal["movements", "interest"],
    index: int,
    fingerprint: Annotated[str, Form()],
    move_date: Annotated[str, Form()],
    base_movement: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    move_type: Annotated[str, Form()] = "",
    acc_debit: Annotated[str, Form()] = "",
    acc_credit: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
    investment: Annotated[str, Form()] = "",
    quantity: Annotated[str, Form()] = "",
    investment_account: Annotated[str, Form()] = "",  # only narrows down the investments of the form
    fees: Annotated[str, Form()] = "",
):
    values = {
        "move_date": move_date, "move_type": move_type, "base_movement": base_movement,
        "acc_debit": acc_debit, "acc_credit": acc_credit,
        "amount": amount, "comment": comment,
        "investment": investment, "quantity": quantity, "investment_account": investment_account,
    }
    field = INVEST_SOURCES[source]
    editing, _ = editing_of(store, field, index, fingerprint, f"/investments/{source}/{index}/edit")
    try:
        if fees.strip():
            raise ValueError("Add the broker fees of a movement already saved as their own movement")
        store.edit_investment(
            field, index, fingerprint, move_date, base_movement, acc_debit, acc_credit, amount, comment,
            investment, quantity,
        )
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        return render_invest_form(request, store.load(), values, error=str(e), editing=editing)
    of = f" of {investment.strip()}" if investment.strip() else ""
    value = money(float(amount.replace(",", "")), store.settings.currency)
    response = render_invest_form(
        request, store.load(), {}, success=updated(editing, f"{base_movement}{of}: {value}", move_date)
    )
    response.headers["HX-Trigger"] = "investments-changed"
    return response


@router.get("/form/conversion")
def conversion_form(request: Request, store: StoreDep):
    """A new conversion form, e.g. to cancel editing one."""
    return render_conversion_form(request, store.load(), {})


@router.get("/conversions/{index}/edit")
def edit_conversion_form(request: Request, store: StoreDep, index: int, fingerprint: str):
    """
    The conversion form filled with a row of the conversions CSV, to edit it: with both
    rows of a cross conversion (see store.conversion_pairs).
    """
    editing, snapshot = editing_of(store, "conversions", index, fingerprint)
    conversions = {c["index"]: c for c in build_conversions(snapshot)}
    c = conversions[index]
    if c["pair"] is not None:
        # Its Foreign currency - Sold row, then its Foreign currency - Bought one
        out, into = sorted([c, conversions[c["pair"]]], key=lambda r: r["index"])
        values = {
            "move_date": c["date"], "sold": out["currency"], "bought": into["currency"], "acc_from": out["credit"],
            "acc_to": into["debit"], "amount_sold": f"{out['other_amount']:,.4f}",
            "amount_bought": f"{into['other_amount']:,.4f}", "comment": c["comment"],
        }
        return render_conversion_form(request, snapshot, values, editing=editing)
    base, other = snapshot.base_currency.name, c["currency"]
    base_amount, other_amount = f"{c['amount']:,.2f}", f"{c['other_amount']:,.4f}"
    # The account to (debit) gets the currency bought, from the account from (credit).
    sold, bought, amount_sold, amount_bought = (
        (base, other, base_amount, other_amount) if c["bought"] else (other, base, other_amount, base_amount)
    )
    values = {
        "move_date": c["date"], "sold": sold, "bought": bought, "acc_from": c["credit"], "acc_to": c["debit"],
        "amount_sold": amount_sold, "amount_bought": amount_bought, "comment": c["comment"],
    }
    return render_conversion_form(request, snapshot, values, editing=editing)


@router.post("/conversions/{index}/edit")
def edit_conversion(
    request: Request,
    store: StoreDep,
    index: int,
    fingerprint: Annotated[str, Form()],
    move_date: Annotated[str, Form()],
    sold: Annotated[str, Form()],
    bought: Annotated[str, Form()],
    amount_sold: Annotated[str, Form()],
    amount_bought: Annotated[str, Form()],
    acc_from: Annotated[str, Form()] = "",
    acc_to: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
):
    values = {
        "move_date": move_date, "sold": sold, "bought": bought, "acc_from": acc_from, "acc_to": acc_to,
        "amount_sold": amount_sold, "amount_bought": amount_bought, "comment": comment,
    }
    editing, _ = editing_of(store, "conversions", index, fingerprint)
    try:
        base_movement = store.edit_exchange(
            index, fingerprint, move_date, sold, bought, acc_from, acc_to, amount_sold, amount_bought, comment
        )
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        return render_conversion_form(request, store.load(), values, error=str(e), editing=editing)
    what = (
        f"{base_movement}: {float(amount_sold.replace(',', '')):,.2f} {sold} sold for "
        f"{float(amount_bought.replace(',', '')):,.2f} {bought}"
    )
    response = render_conversion_form(request, store.load(), {}, success=updated(editing, what, move_date))
    response.headers["HX-Trigger"] = "conversions-changed"
    return response


@router.get("/form/debt-bases")
def form_debt_bases(request: Request, store: StoreDep, debt_kind: str = ""):
    """The debt base movements of a kind of the Debt tab (see views.debt_form_fields)."""
    return templates.TemplateResponse(
        request, "_debt_bases.html", {**debt_form_fields(store.load(), kind=debt_kind), "values": {}}
    )


@router.get("/form/debt-difference")
def form_debt_difference(
    request: Request, store: StoreDep, base_movement: str = "", who: str = "", amount: str = "",
    remaining_as_difference: bool = False,
):
    """What the debt movement form would leave with someone, and the Difference settling it."""
    context = debt_difference(store.load(), base_movement, who, amount) if remaining_as_difference else {}
    return templates.TemplateResponse(request, "_debt_difference.html", {"asked": remaining_as_difference, **context})


@router.get("/form/debt-movement")
def debt_movement_form(request: Request, store: StoreDep):
    """A new debt movement form, e.g. to cancel editing one."""
    return render_debt_form(request, store.load(), {})


@router.get("/debt/movements/{index}/edit")
def edit_debt_movement_form(request: Request, store: StoreDep, index: int, fingerprint: str):
    """
    The debt movement form filled with a row of the ledger CSV, to edit it: who its
    comment names, and the rest of the comment.
    """
    editing, snapshot = editing_of(store, "rows", index, fingerprint)
    values = ledger_values(snapshot.rows.iloc[index])
    debts = [a for n in (values["acc_debit"], values["acc_credit"]) if isinstance(a := snapshot.accounts.get(n), DebtAccount)]
    who = debts[0].creditor_in(values["comment"]) if debts else None
    if who:
        comment = values["comment"]
        rest = comment[len(who):].lstrip(":").strip() if comment.lower().startswith(who.lower()) else comment
        values |= {"who": who, "comment": rest}
    return render_debt_form(request, snapshot, values, editing=editing)


@router.post("/debt/movements/{index}/edit")
def edit_debt_movement(
    request: Request,
    store: StoreDep,
    index: int,
    fingerprint: Annotated[str, Form()],
    move_date: Annotated[str, Form()],
    base_movement: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    who: Annotated[str, Form()] = "",
    acc_debit: Annotated[str, Form()] = "",
    acc_credit: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
):
    values = {
        "move_date": move_date, "base_movement": base_movement, "acc_debit": acc_debit,
        "acc_credit": acc_credit, "amount": amount, "who": who, "comment": comment,
    }
    editing, _ = editing_of(store, "rows", index, fingerprint)
    try:
        store.edit_debt_movement(index, fingerprint, move_date, base_movement, acc_debit, acc_credit, amount, who, comment)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        return render_debt_form(request, store.load(), values, error=str(e), editing=editing)
    value = money(float(amount.replace(",", "")), store.settings.currency)
    response = render_debt_form(
        request, store.load(), {}, success=updated(editing, f"{base_movement} ({who.strip()}): {value}", move_date)
    )
    response.headers["HX-Trigger"] = "debt-changed"
    return response


@router.get("/form/card-payment")
def card_payment_form(request: Request, store: StoreDep, base_movement: str = ""):
    """A new card payment form, e.g. to cancel editing one."""
    return render_card_payment_form(request, store.load(), {"base_movement": base_movement})


@router.get("/credit-cards/payments/{index}/edit")
def edit_card_payment_form(request: Request, store: StoreDep, index: int, fingerprint: str):
    """The card payment form filled with a row of the ledger CSV, to edit it."""
    editing, snapshot = editing_of(store, "rows", index, fingerprint)
    row = snapshot.rows.iloc[index]
    values = {**ledger_values(row), "amount_card": row.get(OTHER_AMOUNT, "")}
    return render_card_payment_form(request, snapshot, values, editing=editing)


@router.post("/credit-cards/payments/{index}/edit")
def edit_card_payment(
    request: Request,
    store: StoreDep,
    index: int,
    fingerprint: Annotated[str, Form()],
    move_date: Annotated[str, Form()],
    base_movement: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    acc_debit: Annotated[str, Form()] = "",
    acc_credit: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
    amount_card: Annotated[str, Form()] = "",
):
    values = {
        "move_date": move_date, "base_movement": base_movement, "acc_debit": acc_debit,
        "acc_credit": acc_credit, "amount": amount, "comment": comment, "amount_card": amount_card,
    }
    editing, _ = editing_of(store, "rows", index, fingerprint)
    try:
        store.edit_card_payment(index, fingerprint, move_date, base_movement, acc_debit, acc_credit, amount, comment, amount_card)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        return render_card_payment_form(request, store.load(), values, error=str(e), editing=editing)
    value = money(float(amount.replace(",", "")), store.settings.currency)
    response = render_card_payment_form(
        request, store.load(), {}, success=updated(editing, f"payment of {acc_debit}: {value}", move_date)
    )
    response.headers["HX-Trigger"] = "credit-cards-changed"
    return response


@router.post("/debt/movements/{index}/delete", response_class=Response)
def delete_debt_movement(store: StoreDep, index: int, fingerprint: Annotated[str, Form()]):
    try:
        store.delete_debt_movement(index, fingerprint)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return Response(status_code=200, headers={"HX-Trigger": "debt-changed"})


@router.post("/credit-cards/payments/{index}/delete", response_class=Response)
def delete_card_payment(store: StoreDep, index: int, fingerprint: Annotated[str, Form()]):
    try:
        store.delete(index, fingerprint, card_payment=True)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return Response(status_code=200, headers={"HX-Trigger": "credit-cards-changed"})
