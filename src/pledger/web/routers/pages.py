"""pages.py"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from pledger.web.dependencies import StoreDep
from pledger.web.templating import (
    is_partial,
    remember_month,
    remembered_month,
    render_card_payment_form,
    render_conversion_form,
    render_debt_form,
    render_form,
    render_invest_form,
    render_new_investment_form,
    render_price_form,
    render_service_form,
    templates,
)
from pledger.web.views import (
    audit_form,
    build_accounts,
    build_credit_cards,
    build_currencies,
    build_debts,
    build_invest_movements,
    build_investments,
    build_results,
    build_services,
    build_view,
    build_work,
    conversion_filter,
    invest_filter,
    months_of,
    setup_context,
)

router = APIRouter(tags=["pages"], default_response_class=HTMLResponse)


@router.get("/")
def index(
    request: Request, store: StoreDep, month: str | None = None, account: str = "",
    initial: bool = False, base: str = "", q: str = "",
):
    snapshot = store.load()
    month = remembered_month(request, month, months_of(snapshot))
    view = build_view(snapshot, month, account, initial, base, q)
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


@router.get("/services")
def services(request: Request, store: StoreDep, service: str = ""):
    snapshot = store.load()
    context = {**build_services(snapshot, service), "settings": store.settings}
    if is_partial(request):
        return templates.TemplateResponse(request, "_services.html", context)
    context["service_form"] = render_service_form(request, snapshot).body.decode()
    return templates.TemplateResponse(request, "services.html", context)


@router.get("/investments")
def investments(request: Request, store: StoreDep, account: str = "", investment: str = "", base: str = ""):
    snapshot = store.load()
    context = {
        **build_investments(snapshot),
        "settings": store.settings,
        **invest_filter(build_invest_movements(snapshot), account, investment, base),
        "invest_form": (
            render_invest_form(request, snapshot, {}).body.decode()
            if snapshot.invest_base_movements() else None
        ),
        "new_investment_form": render_new_investment_form(request, snapshot, {}).body.decode(),
        "price_form": render_price_form(request, snapshot, {}).body.decode(),
    }
    return templates.TemplateResponse(request, "investments.html", context)


@router.get("/debt")
def debt(request: Request, store: StoreDep, month: str | None = None, who: str = ""):
    snapshot = store.load()
    month = remembered_month(request, month, months_of(snapshot))
    context = {**build_debts(snapshot, month, who), "settings": store.settings}
    if is_partial(request):
        return remember_month(templates.TemplateResponse(request, "_debt.html", context), month)
    context["debt_form"] = render_debt_form(request, snapshot, {}).body.decode()
    return remember_month(templates.TemplateResponse(request, "debt.html", context), month)


@router.get("/credit-cards")
def credit_cards(request: Request, store: StoreDep, month: str | None = None, card: str = ""):
    snapshot = store.load()
    month = remembered_month(request, month, months_of(snapshot))
    context = {**build_credit_cards(snapshot, month, card), "settings": store.settings}
    if is_partial(request):
        return remember_month(templates.TemplateResponse(request, "_credit_cards.html", context), month)
    context["card_payment_form"] = render_card_payment_form(request, snapshot, {}).body.decode()
    return remember_month(templates.TemplateResponse(request, "credit_cards.html", context), month)


@router.get("/work")
def work(request: Request, store: StoreDep):
    context = build_work(store.load_work(), cpi=store.load_cpi())
    return templates.TemplateResponse(
        request, "work.html", {**context, "settings": store.settings}
    )


@router.get("/currencies")
def currencies(request: Request, store: StoreDep, currency: str = "", account: str = ""):
    snapshot = store.load()
    context = {
        **build_currencies(snapshot),
        "settings": store.settings,
        **conversion_filter(snapshot, currency, account),
        "conversion_form": (
            render_conversion_form(request, snapshot, {}).body.decode() if len(snapshot.currencies) > 1 else None
        ),
    }
    return templates.TemplateResponse(request, "currencies.html", context)


@router.get("/results")
def results(request: Request, store: StoreDep, month: str | None = None, initial: bool = False):
    snapshot = store.load()
    month = remembered_month(request, month, months_of(snapshot))
    context = build_results(snapshot, month, initial)
    if is_partial(request):
        return remember_month(templates.TemplateResponse(request, "_results.html", context), month)
    context = {**context, **setup_context(snapshot), "settings": store.settings}
    return remember_month(templates.TemplateResponse(request, "results.html", context), month)


@router.get("/audit")
def audit(request: Request, store: StoreDep):
    return templates.TemplateResponse(request, "audit.html", audit_form(store.load()))


# The sections of the Help tab, in reading order: the id of its page and template, its emoji and title
HELP_SECTIONS = [
    ("start", "🚀", "Getting started"),
    ("tabs", "🧭", "The tabs"),
    ("concepts", "💡", "Key ideas"),
    ("how-to", "🛠️", "How to…"),
    ("examples", "🧾", "Examples"),
    ("glossary", "📖", "Glossary"),
    ("faq", "❓", "Questions"),
    ("about", "🐙", "Source code and license"),
]


@router.get("/help")
@router.get("/help/{section}")
def help_page(request: Request, store: StoreDep, section: str = HELP_SECTIONS[0][0]):
    number = next((n for n, s in enumerate(HELP_SECTIONS, 1) if s[0] == section), None)
    if number is None:
        raise HTTPException(status_code=404, detail=f"No help section {section!r}")
    return templates.TemplateResponse(request, "help.html", {
        "settings": store.settings, "sections": HELP_SECTIONS, "number": number,
        "section": HELP_SECTIONS[number - 1],
        "previous": HELP_SECTIONS[number - 2] if number > 1 else None,
        "next": HELP_SECTIONS[number] if number < len(HELP_SECTIONS) else None,
    })
