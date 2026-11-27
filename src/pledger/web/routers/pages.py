"""pages.py"""

from __future__ import annotations

from typing import Annotated, NamedTuple

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from pledger.enums import CreditorKind, DebtRole
from pledger.web.dependencies import StoreDep
from pledger.web.store import LedgerDataError
from pledger.web.templating import (
    is_partial,
    remember_month,
    remembered_month,
    render_card_payment_form,
    render_collection_form,
    render_conversion_form,
    render_currency_week_form,
    render_debt_currency_form,
    render_debt_exchange_form,
    render_debt_form,
    render_form,
    render_invest_form,
    render_new_investment_form,
    render_person_form,
    render_prices_form,
    render_service_form,
    render_service_payment_form,
    NAV,
    templates,
)
from pledger.web.views import (
    audit_form,
    audit_history,
    build_accounts,
    build_credit_cards,
    build_currencies,
    build_dashboard,
    build_debts,
    build_invest_movements,
    build_investments,
    build_results,
    build_services,
    build_view,
    build_work,
    build_work_loan,
    conversion_filter,
    expense_months,
    invest_filter,
    months_of,
    new_movement_buttons,
    price_rows,
    setup_context,
)

router = APIRouter(tags=["pages"], default_response_class=HTMLResponse)


@router.get("/")
def dashboard(request: Request, store: StoreDep):
    """The Dashboard tab: the New movement buttons, and the number cards and plots in its slots."""
    snapshot = store.load()
    context = {**build_dashboard(snapshot, store.load_dashboard()), "new_movements": new_movement_buttons(snapshot)}
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/ledger")
def ledger(
    request: Request, store: StoreDep, month: str | None = None, account: str = "",
    initial: bool = False, base: str = "", q: str = "", editable: bool = False,
):
    snapshot = store.load()
    month = remembered_month(request, month, months_of(snapshot))
    view = build_view(snapshot, month, account, initial, base, q, editable)
    if is_partial(request):
        return remember_month(templates.TemplateResponse(request, "_view.html", view), month)
    form = render_form(request, snapshot, {}).body.decode()
    return remember_month(
        templates.TemplateResponse(request, "ledger.html", {**view, "form": form}), month
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
    context["service_payment_form"] = render_service_payment_form(request, snapshot).body.decode()
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
        "prices_form": render_prices_form(request, snapshot).body.decode(),
        "has_prices": bool(price_rows(snapshot)),
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
    context["exchange_form"] = render_debt_exchange_form(request, snapshot, {}).body.decode()
    currency_form = render_debt_currency_form(request, snapshot, {})
    context["currency_form"] = currency_form.body.decode() if currency_form.context["available"] else ""
    context["person_form"] = render_person_form(request, {}).body.decode()
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
    # The work history doesn't need the rest of the ledger: what the companies owe does
    try:
        snapshot = store.load()
    except LedgerDataError:
        snapshot = None
    if snapshot is not None and snapshot.base_debt_account(DebtRole.DEBTOR, CreditorKind.COMPANY) is not None:
        context["loan"] = build_work_loan(snapshot)
        context["collection_form"] = render_collection_form(request, snapshot).body.decode()
    return templates.TemplateResponse(
        request, "work.html", {**context, "settings": store.settings}
    )


@router.get("/work/loan")
def work_loan(request: Request, store: StoreDep):
    """What the companies owe in Wo, for the Work tab."""
    return templates.TemplateResponse(request, "_work_loan.html", {"loan": build_work_loan(store.load())})


@router.get("/currencies")
def currencies(request: Request, store: StoreDep, currency: str = "", account: str = "", initial: bool = False):
    snapshot = store.load()
    context = {
        **build_currencies(snapshot),
        "settings": store.settings,
        **conversion_filter(snapshot, currency, account, initial),
        "conversion_form": (
            render_conversion_form(request, snapshot, {}).body.decode() if len(snapshot.currencies) > 1 else None
        ),
        "week_form": (
            render_currency_week_form(request, snapshot).body.decode() if len(snapshot.currencies) > 1 else None
        ),
    }
    return templates.TemplateResponse(request, "currencies.html", context)


# The loss result types of the Equity in months of expenses chart, once its filter was used
# (`expenses_filtered`, even with none of them): the non-built-in ones by default.
ExpenseTypes = Annotated[list[str] | None, Query()]


@router.get("/results")
def results(
    request: Request, store: StoreDep, month: str | None = None, initial: bool = False,
    expense_types: ExpenseTypes = None, expenses_filtered: bool = False,
):
    snapshot = store.load()
    month = remembered_month(request, month, months_of(snapshot))
    context = build_results(snapshot, month, initial, (expense_types or []) if expenses_filtered else None)
    if is_partial(request):
        return remember_month(templates.TemplateResponse(request, "_results.html", context), month)
    context = {**context, **setup_context(snapshot), "settings": store.settings}
    return remember_month(templates.TemplateResponse(request, "results.html", context), month)


@router.get("/results/expenses")
def results_expenses(
    request: Request, store: StoreDep, month: str = "", initial: bool = False,
    expense_types: ExpenseTypes = None, expenses_filtered: bool = False,
):
    """The Equity in months of expenses panel alone, as its filter changes."""
    snapshot = store.load()
    selected = (expense_types or []) if expenses_filtered else None
    context = {"month": month, "expenses": expense_months(snapshot, month, selected)}
    return templates.TemplateResponse(request, "_expenses.html", context)


@router.get("/audit")
def audit(request: Request, store: StoreDep):
    snapshot = store.load()
    context = {**audit_form(snapshot), "history": audit_history(snapshot, store.load_audits())}
    return templates.TemplateResponse(request, "audit.html", context)


# The tabs, each with a page of its own in "The tabs" section of the Help tab, in the order of the
# top bar (see NAV): the id of its page and template, its emoji, title, path and the section of the
# top bar it is in (None for the tabs always in sight)
HELP_TABS = [(*tab, section) for section, tabs in NAV for tab in tabs]

# The sections of the Help tab, in reading order: the id of its page and template, its emoji,
# title and subsections (each a page of its own, in help/<section>/<subsection>.html)
HELP_SECTIONS = [
    ("start", "🚀", "Getting started", ()),
    ("concepts", "💡", "Key ideas", ()),
    ("examples", "🧾", "Examples", ()),
    ("tabs", "🧭", "The tabs", tuple(t[:3] for t in HELP_TABS)),
    ("how-to", "🛠️", "How to…", ()),
    ("glossary", "📖", "Glossary", ()),
    ("faq", "❓", "Questions", ()),
    ("about", "🐙", "Source code and license", ()),
]


class HelpPage(NamedTuple):
    """A page of the Help tab: a section, or a subsection of one, numbered like "4" or "4.2"."""
    path: str  # after /help/, and of its template in help/
    emoji: str
    title: str
    number: str
    section: str  # the id of its section, its own for a section


# Every page of the Help tab in reading order, each section followed by its subsections
HELP_PAGES = [
    page
    for n, (id_, emoji, title, subsections) in enumerate(HELP_SECTIONS, 1)
    for page in (
        HelpPage(id_, emoji, title, str(n), id_),
        *(HelpPage(f"{id_}/{sub}", e, t, f"{n}.{m}", id_) for m, (sub, e, t) in enumerate(subsections, 1)),
    )
]
HELP_INDEX = {page.path: i for i, page in enumerate(HELP_PAGES)}


@router.get("/help")
@router.get("/help/{path:path}")
def help_page(request: Request, store: StoreDep, path: str = HELP_PAGES[0].path):
    i = HELP_INDEX.get(path)
    if i is None:
        raise HTTPException(status_code=404, detail=f"No help page {path!r}")
    page = HELP_PAGES[i]
    return templates.TemplateResponse(request, "help.html", {
        "settings": store.settings, "sections": HELP_SECTIONS, "page": page,
        "parent": HELP_PAGES[HELP_INDEX[page.section]] if page.path != page.section else None,
        "help_tab": next((t for t in HELP_TABS if f"tabs/{t[0]}" == page.path), None), "tabs": HELP_TABS,
        # The subsections of the index, the tabs in the sections of the top bar
        "toc_groups": (
            [(section, [tab[:3] for tab in tabs]) for section, tabs in NAV] if page.section == "tabs"
            else [(None, next(subs for id_, _, _, subs in HELP_SECTIONS if id_ == page.section))]
        ),
        "previous": HELP_PAGES[i - 1] if i > 0 else None,
        "next": HELP_PAGES[i + 1] if i + 1 < len(HELP_PAGES) else None,
    })
