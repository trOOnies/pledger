"""templating.py"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from fastapi import Request, Response
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from pledger.enums import MovementType
from pledger.models import CreditCardAccount, is_foreign
from pledger.web.store import MAX_INSTALLMENTS, Snapshot
from pledger.web.views import (
    MOVEMENT_SOURCES,
    base_fields,
    card_payment_fields,
    conversion_fields,
    debt_form_fields,
    invest_fields,
    ledger_link,
    new_investment_fields,
    price_rows,
    row_anchor,
    service_form,
    service_payment_form,
)

HERE = Path(__file__).parent


def money(value: float, currency: str = "$") -> str:
    """Format an amount as currency with cents, e.g. -$1,234.50 or ARS 1,234.50."""
    value = round(value, 2)
    sign = "-" if value < 0 else ""
    sep = " " if currency[-1:].isalpha() else ""
    return f"{sign}{currency}{sep}{abs(value):,.2f}"


@pass_context
def money_filter(context, value: float) -> str:
    return money(value, context["request"].app.state.store.settings.currency)


@pass_context
def money_in_filter(context, value: float, currency: str | None = None) -> str:
    """An amount in this currency, e.g. USD 1,234.50: in the base one (see money_filter) without one."""
    return money(value, currency) if currency else money_filter(context, value)


templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["money"] = money_filter
templates.env.filters["money_in"] = money_in_filter


def quantity(value: float) -> str:
    """Format a quantity with up to 4 decimals and no trailing zeros, e.g. 1,234.5."""
    return f"{value:,.4f}".rstrip("0").rstrip(".")


templates.env.filters["quantity"] = quantity


def number(value: float) -> str:
    """Format a value that isn't an amount, like a CPI or an exchange rate, e.g. 1,234.50."""
    return f"{value:,.2f}"


def signed_percent(value: float | None) -> str:
    """Format a ratio as a signed percentage, e.g. 0.0123 -> +1.23%, or - without one."""
    return "-" if value is None else f"{100 * value:+.2f}%"


templates.env.filters["number"] = number
templates.env.filters["signed_percent"] = signed_percent


def static_version(path: str) -> int:
    """A version for static URLs that changes with the file, so browsers never use a stale copy."""
    return int((HERE / "static" / path).stat().st_mtime)


templates.env.globals["static_version"] = static_version
templates.env.globals["movement_sources"] = MOVEMENT_SOURCES
templates.env.globals["row_anchor"] = row_anchor
templates.env.globals["ledger_link"] = ledger_link


def is_partial(request: Request) -> bool:
    return (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-History-Restore-Request") != "true"
    )


MONTH_COOKIE = "pledger_month"


def remembered_month(request: Request, month: str | None, months: list[str]) -> str:
    """
    The month filter shared by the Ledger, Accounts, Results, Debt and Credit Cards
    pages: the one in the URL if given (empty for all time), else the last one
    picked, if it still has movements.
    """
    if month is None:
        month = request.cookies.get(MONTH_COOKIE, "")
        if month not in months:
            month = ""
    return month if re.fullmatch(r"\d{4}-\d{2}", month) else ""


def remember_month(response: Response, month: str) -> Response:
    response.set_cookie(MONTH_COOKIE, month, max_age=365 * 24 * 3600, httponly=True, samesite="lax")
    return response


def render_form(request: Request, snapshot: Snapshot, values: dict, **extra):
    values.setdefault("move_date", dt.date.today().isoformat())
    base = base_fields(snapshot, values.get("move_type", ""), values.get("base_movement", ""))
    values["move_type"] = base["move_type"]
    values["base_movement"] = base["base_movement"]
    return templates.TemplateResponse(
        request,
        "_form.html",
        {
            "move_types": [t.value for t in MovementType],
            "foreign_accounts": {a.name: a.currency.name for a in snapshot.accounts.values() if is_foreign(a)},
            "credit_cards": [a.name for a in snapshot.accounts.values() if isinstance(a, CreditCardAccount)],
            "max_installments": MAX_INSTALLMENTS,
            "currency": request.app.state.store.settings.currency,
            "values": values,
            **base,
            **extra,
        },
    )


def render_invest_form(request: Request, snapshot: Snapshot, values: dict, **extra):
    """The new movement form of the Investments tab, for the INVEST base movements."""
    values.setdefault("move_date", dt.date.today().isoformat())
    invest = invest_fields(snapshot, values.get("base_movement", ""), values.get("move_type", ""))
    values["base_movement"] = invest["base_movement"]
    values["move_type"] = invest["move_type"]
    values["currency"] = values.get("currency") or snapshot.base_currency.name
    return templates.TemplateResponse(
        request,
        "_invest_form.html",
        {
            "currency": request.app.state.store.settings.currency,
            "values": values,
            **invest,
            **extra,
        },
    )


def render_new_investment_form(request: Request, snapshot: Snapshot, values: dict, **extra):
    """The new investment form of the Investments tab, for the investments YAML."""
    settings = request.app.state.store.settings
    return templates.TemplateResponse(
        request,
        "_new_investment_form.html",
        {
            "currency": settings.currency,
            "path": settings.investments_path.name if settings.investments_path else "investment.yaml",
            "prices_path": settings.prices_path.name if settings.prices_path else "prices.csv",
            "values": values,
            **new_investment_fields(snapshot),
            **extra,
        },
    )


def render_prices_form(
    request: Request, snapshot: Snapshot, date: str = "", entered: dict[tuple[str, str], str] | None = None, **extra,
):
    """The prices form of the Investments tab, for the prices CSV: today's by default."""
    settings = request.app.state.store.settings
    return templates.TemplateResponse(
        request,
        "_prices_form.html",
        {
            "currency": settings.currency,
            "path": settings.prices_path.name if settings.prices_path else "prices.csv",
            "date": date or dt.date.today().isoformat(),
            "rows": price_rows(snapshot, entered),
            **extra,
        },
    )


def render_service_form(
    request: Request, snapshot: Snapshot, month: str = "", values: dict | None = None, **extra
):
    """The month form of the Services tab, for the services CSV (see views.service_form)."""
    settings = request.app.state.store.settings
    return templates.TemplateResponse(
        request,
        "_service_form.html",
        {
            "currency": settings.currency,
            "path": settings.services_path.name if settings.services_path else "services.csv",
            **service_form(snapshot, month, values),
            **extra,
        },
    )


def render_service_payment_form(
    request: Request, snapshot: Snapshot, service: str = "", month: str = "", values: dict | None = None, **extra
):
    """The payment form of the Services tab, for a month of a service (see views.service_payment_form)."""
    values = dict(values or {})
    values.setdefault("move_date", dt.date.today().isoformat())
    return templates.TemplateResponse(
        request,
        "_service_payment_form.html",
        {
            "currency": request.app.state.store.settings.currency,
            **service_payment_form(snapshot, service, month, values),
            **extra,
        },
    )


def render_conversion_form(request: Request, snapshot: Snapshot, values: dict, **extra):
    """The new conversion form of the Currencies tab, for the conversions CSV."""
    values.setdefault("move_date", dt.date.today().isoformat())
    fields = conversion_fields(snapshot, values.get("sold", ""), values.get("bought", ""))
    values["sold"], values["bought"] = fields["sold"], fields["bought"]
    return templates.TemplateResponse(
        request,
        "_conversion_form.html",
        {
            "currency": request.app.state.store.settings.currency,
            "base_currency": snapshot.base_currency.name,
            "values": values,
            **fields,
            **extra,
        },
    )


def render_debt_form(request: Request, snapshot: Snapshot, values: dict, **extra):
    """The new debt movement form of the Debt tab, for the debt base movements."""
    values.setdefault("move_date", dt.date.today().isoformat())
    fields = debt_form_fields(snapshot, values.get("base_movement", ""))
    values["base_movement"] = fields["base_movement"]
    return templates.TemplateResponse(
        request,
        "_debt_form.html",
        {"currency": request.app.state.store.settings.currency, "values": values, **fields, **extra},
    )


def render_card_payment_form(request: Request, snapshot: Snapshot, values: dict, **extra):
    """The new card payment form of the Credit Cards tab, for the card payment base movements."""
    values.setdefault("move_date", dt.date.today().isoformat())
    fields = card_payment_fields(snapshot, values.get("base_movement", ""))
    values["base_movement"] = fields["base_movement"]
    return templates.TemplateResponse(
        request,
        "_card_payment_form.html",
        {"currency": request.app.state.store.settings.currency, "values": values, **fields, **extra},
    )
