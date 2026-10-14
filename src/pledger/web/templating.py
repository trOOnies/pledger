"""templating.py"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import Request
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


def is_partial(request: Request) -> bool:
    return (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-History-Restore-Request") != "true"
    )


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
