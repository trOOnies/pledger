"""templating.py"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from pledger.enums import MovementType
from pledger.web.store import Snapshot
from pledger.web.views import account_fields

HERE = Path(__file__).parent


def money(value: float) -> str:
    return f"{value:,.2f}"


templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["money"] = money


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
