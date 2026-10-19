"""pages.py"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from pledger.web.dependencies import StoreDep
from pledger.web.templating import (
    is_partial,
    remember_month,
    remembered_month,
    render_form,
    templates,
)
from pledger.web.views import (
    build_accounts,
    build_charts,
    build_debts,
    build_investments,
    build_view,
    build_work,
    months_of,
    setup_context,
)

router = APIRouter(tags=["pages"], default_response_class=HTMLResponse)


@router.get("/")
def index(
    request: Request, store: StoreDep, month: str | None = None, account: str = "",
    initial: bool = False,
):
    snapshot = store.load()
    month = remembered_month(request, month, months_of(snapshot))
    view = build_view(snapshot, month, account, initial)
    if is_partial(request):
        return remember_month(templates.TemplateResponse(request, "_view.html", view), month)
    form = render_form(request, snapshot, {}).body.decode()
    return remember_month(
        templates.TemplateResponse(request, "index.html", {**view, "form": form}), month
    )


@router.get("/accounts")
def accounts(request: Request, store: StoreDep, month: str | None = None):
    snapshot = store.load()
    month = remembered_month(request, month, months_of(snapshot))
    context = build_accounts(snapshot, month)
    template = "_accounts.html" if is_partial(request) else "accounts.html"
    return remember_month(templates.TemplateResponse(request, template, context), month)


@router.get("/investments")
def investments(request: Request, store: StoreDep):
    context = build_investments(store.load())
    return templates.TemplateResponse(
        request, "investments.html", {**context, "settings": store.settings}
    )


@router.get("/debt")
def debt(request: Request, store: StoreDep, month: str | None = None):
    snapshot = store.load()
    month = remembered_month(request, month, months_of(snapshot))
    context = {**build_debts(snapshot, month), "settings": store.settings}
    template = "_debt.html" if is_partial(request) else "debt.html"
    return remember_month(templates.TemplateResponse(request, template, context), month)


@router.get("/work")
def work(request: Request, store: StoreDep):
    context = build_work(store.load_work())
    return templates.TemplateResponse(
        request, "work.html", {**context, "settings": store.settings}
    )


@router.get("/charts")
def charts(request: Request, store: StoreDep, month: str | None = None, initial: bool = False):
    snapshot = store.load()
    month = remembered_month(request, month, months_of(snapshot))
    context = build_charts(snapshot, month, initial)
    template = "_charts.html" if is_partial(request) else "charts.html"
    return remember_month(templates.TemplateResponse(request, template, context), month)


@router.get("/help")
def help_page(request: Request, store: StoreDep):
    return templates.TemplateResponse(request, "help.html", {"settings": store.settings})


@router.get("/setup")
def setup(request: Request, store: StoreDep):
    context = setup_context(store.load())
    return templates.TemplateResponse(
        request, "setup.html", {**context, "settings": store.settings}
    )
