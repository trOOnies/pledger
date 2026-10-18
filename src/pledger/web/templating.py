"""templating.py"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from fastapi import Request, Response
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from pledger.enums import MovementType
from pledger.web.store import Snapshot
from pledger.web.views import base_fields

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


templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["money"] = money_filter


def quantity(value: float) -> str:
    """Format a quantity with up to 4 decimals and no trailing zeros, e.g. 1,234.5."""
    return f"{value:,.4f}".rstrip("0").rstrip(".")


templates.env.filters["quantity"] = quantity


def static_version(path: str) -> int:
    """A version for static URLs that changes with the file, so browsers never use a stale copy."""
    return int((HERE / "static" / path).stat().st_mtime)


templates.env.globals["static_version"] = static_version


def is_partial(request: Request) -> bool:
    return (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-History-Restore-Request") != "true"
    )


MONTH_COOKIE = "pledger_month"


def remembered_month(request: Request, month: str | None, months: list[str]) -> str:
    """
    The month filter shared by the Ledger, Accounts and Charts pages: the one in
    the URL if given (empty for all time), else the last one picked, if it still
    has movements.
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
            "currency": request.app.state.store.settings.currency,
            "values": values,
            **base,
            **extra,
        },
    )
