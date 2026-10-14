"""pages.py"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from pledger.enums import MovementType
from pledger.web.dependencies import StoreDep
from pledger.web.templating import is_partial, render_form, templates
from pledger.web.views import ACCOUNT_TYPE_ORDER, build_view

router = APIRouter(tags=["pages"], default_response_class=HTMLResponse)


@router.get("/")
def index(request: Request, store: StoreDep, month: str = "", account: str = ""):
    snapshot = store.load()
    view = build_view(snapshot, month, account)
    if is_partial(request):
        return templates.TemplateResponse(request, "_view.html", view)
    form = render_form(request, snapshot, {}).body.decode()
    return templates.TemplateResponse(
        request, "index.html", {**view, "form": form}
    )


@router.get("/setup")
def setup(request: Request, store: StoreDep):
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
