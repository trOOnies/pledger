"""pages.py"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from pledger.web.dependencies import StoreDep
from pledger.web.templating import is_partial, render_form, templates
from pledger.web.views import build_charts, build_view, setup_context

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


@router.get("/charts")
def charts(request: Request, store: StoreDep, month: str = ""):
    context = build_charts(store.load(), month)
    template = "_charts.html" if is_partial(request) else "charts.html"
    return templates.TemplateResponse(request, template, context)


@router.get("/setup")
def setup(request: Request, store: StoreDep):
    context = setup_context(store.load())
    return templates.TemplateResponse(
        request, "setup.html", {**context, "settings": store.settings}
    )
