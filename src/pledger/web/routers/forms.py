"""forms.py: HTMX endpoints behind the movement and setup forms."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from pledger.models import DebtAccount, InvestmentAccount
from pledger.web.dependencies import StoreDep
from pledger.web.store import LedgerStore, Snapshot, StaleRowError
from pledger.web.templating import (
    money,
    render_card_payment_form,
    render_conversion_form,
    render_debt_form,
    render_form,
    render_invest_form,
    render_new_investment_form,
    templates,
)
from pledger.web.views import (
    EDITABLE_ACCOUNT_TYPES,
    account_correcting,
    account_editing,
    account_fields,
    account_form,
    account_values,
    base_fields,
    base_sides,
    build_conversions,
    build_invest_movements,
    build_work,
    conversion_fields,
    conversion_filter,
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
        store.add(move_date, base_movement, acc_debit, acc_credit, amount, comment)
    except ValueError as e:
        return render_form(request, store.load(), values, error=str(e))
    # Keep date, type and base movement to speed up entering similar movements.
    keep = {"move_date": move_date, "move_type": move_type, "base_movement": base_movement}
    value = float(amount.replace(",", ""))
    response = render_form(
        request, store.load(), keep,
        success=f"Added {base_movement}: {money(value, store.settings.currency)}"
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
    """Delete a movement of the Ledger tab, or a balance correction (see LedgerStore.correct_balance)."""
    snapshot = store.load()
    rows = snapshot.rows
    correction = 0 <= index < len(rows) and rows.iloc[index]["base_movement"] in snapshot.correction_base_movements()
    try:
        store.delete(index, fingerprint, correction=correction)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return Response(status_code=200, headers={"HX-Trigger": "ledger-changed"})


@router.get("/form/invest-bases")
def form_invest_bases(request: Request, store: StoreDep, move_type: str = ""):
    return templates.TemplateResponse(
        request, "_invest_bases.html", {**invest_fields(store.load(), move_type=move_type), "values": {}}
    )


@router.get("/form/invest-fields")
def form_invest_fields(request: Request, store: StoreDep, base_movement: str = ""):
    return templates.TemplateResponse(
        request, "_invest_fields.html", {**invest_fields(store.load(), base_movement), "values": {}}
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
):
    values = {
        "move_date": move_date, "move_type": move_type, "base_movement": base_movement,
        "acc_debit": acc_debit, "acc_credit": acc_credit,
        "amount": amount, "comment": comment,
        "investment": investment, "quantity": quantity, "investment_account": investment_account,
    }
    try:
        store.add(
            move_date, base_movement, acc_debit, acc_credit, amount, comment,
            invest=True, investment=investment, quantity=quantity,
        )
    except ValueError as e:
        return render_invest_form(request, store.load(), values, error=str(e))
    # Keep date, type and base movement to speed up entering similar movements.
    keep = {"move_date": move_date, "move_type": move_type, "base_movement": base_movement}
    value = float(amount.replace(",", ""))
    of = f" of {investment.strip()}" if investment.strip() else ""
    response = render_invest_form(
        request, store.load(), keep,
        success=f"Added {base_movement}{of}: {money(value, store.settings.currency)}"
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
    amount: Annotated[str, Form()] = "",
    quantity: Annotated[str, Form()] = "",
):
    values = {"account": account, "name": name, "investment_type": investment_type, "amount": amount, "quantity": quantity}
    try:
        store.add_investment(account, name, investment_type, amount, quantity)
    except ValueError as e:
        return render_new_investment_form(request, store.load(), values, error=str(e))
    # Keep the account and type to speed up adding similar investments.
    response = render_new_investment_form(
        request, store.load(), {"account": account, "investment_type": investment_type},
        success=f"Added {name.strip()} to {account}",
    )
    response.headers["HX-Trigger"] = "investment-added"
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
def conversions(request: Request, store: StoreDep, currency: str = "", account: str = ""):
    """
    The conversions, of the currency and account picked in the filter if any (see
    views.conversion_filter), with the filter itself, out of band.
    """
    return templates.TemplateResponse(
        request, "_conversions.html", {**conversion_filter(store.load(), currency, account), "oob": True}
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
    keep = {"move_date": move_date, "sold": sold, "bought": bought, "acc_from": acc_from, "acc_to": acc_to}
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
):
    values = {
        "move_date": move_date, "base_movement": base_movement, "acc_debit": acc_debit,
        "acc_credit": acc_credit, "amount": amount, "who": who, "comment": comment,
    }
    try:
        store.add_debt_movement(move_date, base_movement, acc_debit, acc_credit, amount, who, comment)
    except ValueError as e:
        return render_debt_form(request, store.load(), values, error=str(e))
    # Keep date and base movement to speed up entering similar movements.
    keep = {"move_date": move_date, "base_movement": base_movement}
    response = render_debt_form(
        request, store.load(), keep,
        success=f"Added {base_movement} ({who.strip()}): {money(float(amount.replace(',', '')), store.settings.currency)}"
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
):
    values = {
        "move_date": move_date, "base_movement": base_movement, "acc_debit": acc_debit,
        "acc_credit": acc_credit, "amount": amount, "comment": comment,
    }
    try:
        store.add(move_date, base_movement, acc_debit, acc_credit, amount, comment, card_payment=True)
    except ValueError as e:
        return render_card_payment_form(request, store.load(), values, error=str(e))
    # Keep date and base movement to speed up entering similar payments.
    keep = {"move_date": move_date, "base_movement": base_movement}
    response = render_card_payment_form(
        request, store.load(), keep,
        success=f"Paid {acc_debit}: {money(float(amount.replace(',', '')), store.settings.currency)}"
        + (" (dated in the future)" if move_date > dt.date.today().isoformat() else ""),
    )
    response.headers["HX-Trigger"] = "credit-cards-changed"
    return response


def render_setup(request: Request, store: LedgerStore, values: dict, **extra):
    return templates.TemplateResponse(
        request,
        "_setup.html",
        {**setup_context(store.load(), values), "settings": store.settings, **extra},
    )


def render_account_form(
    request: Request, store: LedgerStore, values: dict, editing: dict | None = None,
    correcting: dict | None = None, changed: bool = False, **extra,
):
    """
    The account form alone (see views.account_form); `changed` tells the rest of the
    Accounts tab to reload.
    """
    response = templates.TemplateResponse(
        request, "_account_form.html", {**account_form(store.load(), values, editing, correcting), **extra}
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


def correctable_account(store: LedgerStore, name: str):
    """An account whose balance can be corrected in the Accounts tab: not an investment one."""
    account = editable_account(store, name)
    if isinstance(account, InvestmentAccount):
        raise HTTPException(status_code=404, detail=f"{name!r} is an investment account")
    return account


@router.get("/accounts/correct")
def correct_balance_form(request: Request, store: StoreDep, name: str):
    """The account form to correct the balance of an account (see LedgerStore.correct_balance)."""
    account = correctable_account(store, name)
    return render_account_form(request, store, {}, correcting=account_correcting(store.load(), account))


@router.post("/accounts/correct")
def correct_balance(
    request: Request,
    store: StoreDep,
    name: Annotated[str, Form()],
    balance: Annotated[str, Form()] = "",  # required, but checked by the store
    check_date: Annotated[str, Form()] = "",
):
    account = correctable_account(store, name)
    try:
        corrected = store.correct_balance(name, balance, check_date)
    except ValueError as e:
        return render_account_form(
            request, store, {"balance": balance, "check_date": check_date},
            correcting=account_correcting(store.load(), account), account_error=str(e),
        )
    if corrected is None:
        return render_account_form(request, store, {}, account_success=f"The balance of {name} was already right")
    base_movement, amount = corrected
    return render_account_form(
        request, store, {}, changed=True,
        account_success=f"Corrected {name}: {base_movement} of {money(amount, store.settings.currency)}",
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
    """The conversion form filled with a row of the conversions CSV, to edit it."""
    editing, snapshot = editing_of(store, "conversions", index, fingerprint)
    c = next(c for c in build_conversions(snapshot) if c["index"] == index)
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
def card_payment_form(request: Request, store: StoreDep):
    """A new card payment form, e.g. to cancel editing one."""
    return render_card_payment_form(request, store.load(), {})


@router.get("/credit-cards/payments/{index}/edit")
def edit_card_payment_form(request: Request, store: StoreDep, index: int, fingerprint: str):
    """The card payment form filled with a row of the ledger CSV, to edit it."""
    editing, snapshot = editing_of(store, "rows", index, fingerprint)
    return render_card_payment_form(request, snapshot, ledger_values(snapshot.rows.iloc[index]), editing=editing)


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
):
    values = {
        "move_date": move_date, "base_movement": base_movement, "acc_debit": acc_debit,
        "acc_credit": acc_credit, "amount": amount, "comment": comment,
    }
    editing, _ = editing_of(store, "rows", index, fingerprint)
    try:
        store.edit(index, fingerprint, move_date, base_movement, acc_debit, acc_credit, amount, comment, card_payment=True)
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
        store.delete(index, fingerprint, debt=True)
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
