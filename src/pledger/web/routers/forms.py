"""forms.py: HTMX endpoints behind the movement and setup forms."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from pledger.web.dependencies import StoreDep
from pledger.web.store import LedgerStore, StaleRowError
from pledger.web.templating import (
    money,
    remembered_month,
    render_card_payment_form,
    render_conversion_form,
    render_debt_form,
    render_form,
    render_invest_form,
    render_new_investment_form,
    templates,
)
from pledger.web.views import (
    account_fields,
    base_fields,
    base_sides,
    build_accounts,
    build_conversions,
    build_invest_movements,
    build_work,
    conversion_fields,
    invest_fields,
    months_of,
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


@router.post("/movements/{index}/delete", response_class=Response)
def delete_movement(store: StoreDep, index: int, fingerprint: Annotated[str, Form()]):
    try:
        store.delete(index, fingerprint)
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
def investment_movements(request: Request, store: StoreDep):
    return templates.TemplateResponse(
        request, "_invest_movements.html", {"movements": build_invest_movements(store.load())}
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
):
    values = {
        "move_date": move_date, "move_type": move_type, "base_movement": base_movement,
        "acc_debit": acc_debit, "acc_credit": acc_credit,
        "amount": amount, "comment": comment,
        "investment": investment, "quantity": quantity,
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
def conversions(request: Request, store: StoreDep):
    return templates.TemplateResponse(
        request, "_conversions.html", {"conversions": build_conversions(store.load())}
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
        return render_accounts(request, store, values, account_error=str(e))
    return render_accounts(
        request, store, {"acc_type": acc_type, "acc_currency": currency},
        account_success=f"Added account {name.strip()}",
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
