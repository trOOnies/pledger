"""
split.py: the Split tab: the tab of a get-together split among its people (Step 1), what the user
settles now with whom (Step 2), and the movements of their part, checked before they are created
(Step 3). Nothing is saved until then: everything goes in the fields of the form.
"""

from __future__ import annotations

import datetime as dt
import re

from fastapi import APIRouter, HTTPException, Request

from pledger.web.dependencies import StoreDep
from pledger.web.schemas import SplitIn
from pledger.web.store.cart_store import cart_accounts, cart_bases
from pledger.web.store.split_store import people_of
from pledger.web.templating import is_partial, templates
from pledger.web.views.forms import bases_by_group, by_group
from pledger.web.views.split import MAX_OTHERS, YOU, settling_rows, split

router = APIRouter()


def new_values() -> dict:
    """The fields of an empty split: you and one more, one item."""
    return {
        "others": [""], "items": [{"name": "", "price": "", "shares": [], "base": ""}], "paid": ["", ""],
        "percentages": False, "date": dt.date.today().isoformat(), "description": "", "paid_from": "", "settle": {},
    }


def split_page(request: Request, store, values: dict, **extra):
    """The Split tab, or only its content for HTMX: the steps reached, with what their fields offer."""
    snapshot = store.load()
    known, _ = people_of(snapshot)
    result = extra.get("result")
    settling = []
    if result is not None:
        for row in settling_rows(result):
            existing = known.get(row["who"].casefold())
            entered = values["settle"].get(row["who"], ("", ""))
            settling.append({
                **row, "existing": existing, "amount": entered[0] or "0.00", "account": entered[1],
            })
    context = {
        "values": values, "you": YOU, "max_others": MAX_OTHERS,
        "currency": store.settings.currency,
        "people_names": sorted(known.values(), key=str.casefold),
        "account_groups": by_group(snapshot, cart_accounts(snapshot)),
        "base_groups": bases_by_group(snapshot, cart_bases(snapshot)),
        "settling": settling,
        **extra,
    }
    return templates.TemplateResponse(request, "_split.html" if is_partial(request) else "split.html", context)


def form_values(form) -> dict:
    """
    The fields of the split form, in their order. Step 1: the names of the `others`, the items
    (`items-<n>-name`, `-price`, `-base` and `-share-<p>`, p the number of the person, 0 the user),
    `paid` of each one and `percentages`. Step 2: `date`, `description`, `paid_from` and what is settled
    with each one (`settle-<i>-who`, `-amount` and `-account`), as `settle` by who.
    """
    others = [str(v) for v in form.getlist("others")]
    n = len(others) + 1
    numbers = sorted({int(m[1]) for k in form if (m := re.fullmatch(r"items-(\d+)-\w+", k))})
    items = [
        {
            "name": str(form.get(f"items-{i}-name", "")),
            "price": str(form.get(f"items-{i}-price", "")),
            "base": str(form.get(f"items-{i}-base", "")),
            "shares": [str(form.get(f"items-{i}-share-{p}", "")) for p in range(n)],
        }
        for i in numbers
    ]
    settled = sorted({int(m[1]) for k in form if (m := re.fullmatch(r"settle-(\d+)-who", k))})
    return {
        "others": others,
        "items": items,
        "paid": [*[str(v) for v in form.getlist("paid")], *[""] * n][:n],
        "percentages": form.get("percentages") == "true",
        "date": str(form.get("date", "")) or dt.date.today().isoformat(),
        "description": str(form.get("description", "")),
        "paid_from": str(form.get("paid_from", "")),
        "settle": {
            str(form.get(f"settle-{i}-who", "")): (str(form.get(f"settle-{i}-amount", "")), str(form.get(f"settle-{i}-account", "")))
            for i in settled
        },
    }


@router.get("/split", include_in_schema=False)
def split_tab(request: Request, store: StoreDep):
    return split_page(request, store, new_values())


@router.post("/split", include_in_schema=False)
async def split_form(request: Request, store: StoreDep):
    """
    The `action` of the form: `split` (Step 1, then Step 2), `process` (Step 3: the movements to
    create, checked), `settle` (back to Step 2) or `create` (the movements, and a new split).
    """
    form = await request.form()
    action = str(form.get("action", "split"))
    values = form_values(form)
    try:
        result = split(values["others"], values["items"], values["paid"], values["percentages"])
    except ValueError as e:
        return split_page(request, store, values, error=str(e))
    if action not in {"process", "create"}:
        return split_page(request, store, values, result=result)
    step2 = (values["date"], values["description"], values["paid_from"], values["settle"])
    try:
        if action == "create":
            created = store.create_split(result, *step2)
        else:
            plan = store.plan_split(result, *step2)
    except ValueError as e:
        return split_page(request, store, values, result=result, settle_error=str(e))
    if action == "process":
        return split_page(request, store, values, result=result, plan=plan)
    return split_page(request, store, new_values(), created=created)


@router.post("/api/split", tags=["split"])
def create_split(body: SplitIn):
    """
    The split of the tab of a get-together, in cents: what each one fronts and put in, and the
    transfers that settle it. Nothing is saved.
    """
    items = [
        {
            "name": i.name, "price": i.price,
            "shares": [s if body.percentages else ("on" if s else "") for s in i.shares],
        }
        for i in body.items
    ]
    try:
        return split(body.others, items, body.paid, body.percentages)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
