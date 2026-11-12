"""cart.py: the Cart tab: many movements of the ledger at once, kept together as a cart."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request

from pledger.web.dependencies import StoreDep
from pledger.web.store import StaleRowError
from pledger.web.templating import is_partial, money, templates
from pledger.web.views import build_carts, cart_form_of, new_cart_form

router = APIRouter(include_in_schema=False)


def cart_page(request: Request, store, cart_id: str = "", month: str = "", form: dict | None = None, **extra):
    """The Cart tab, or only its content for HTMX."""
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        month = ""
    context = {
        **build_carts(store.load(), store.load_carts(), cart_id, month, form),
        "currency": store.settings.currency, **extra,
    }
    return templates.TemplateResponse(request, "_cart.html" if is_partial(request) else "cart.html", context)


def form_bags(form) -> list[dict]:
    """
    The bags of the cart form, in the order of their numbers: `bags-<n>-base_movement` and
    `bags-<n>-installments`, and a `bags-<n>-id`, `-name` and `-amount` per product.
    """
    numbers = sorted({int(m[1]) for key in form if (m := re.fullmatch(r"bags-(\d+)-base_movement", key))})
    bags = []
    for n in numbers:
        ids, names, amounts = (form.getlist(f"bags-{n}-{k}") for k in ("id", "name", "amount"))
        products = [
            {"id": ids[i] if i < len(ids) else "", "name": names[i] if i < len(names) else "", "amount": amount}
            for i, amount in enumerate(amounts)
        ]
        bags.append({
            "base_movement": form.get(f"bags-{n}-base_movement", ""),
            "installments": form.get(f"bags-{n}-installments", ""),
            "products": products,
        })
    return bags


@router.get("/cart")
def cart(request: Request, store: StoreDep, cart: str = "", month: str = ""):
    return cart_page(request, store, cart, month)


@router.post("/cart/carts")
async def save_cart(request: Request, store: StoreDep):
    form = await request.form()
    cart_id, month = str(form.get("cart_id", "")).strip(), str(form.get("month", ""))
    values = {
        "id": cart_id, "date": form.get("date", ""), "store": form.get("store", ""),
        "account": form.get("account", ""), "bags": form_bags(form),
    }
    try:
        saved = store.save_cart(cart_id, values["date"], values["store"], values["account"], values["bags"])
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        return cart_page(request, store, cart_id, month, values, cart_error=str(e))
    total = sum(
        float(str(p["amount"]).replace(",", "") or 0) for b in values["bags"] for p in b["products"]
        if str(p["amount"]).strip()
    )
    what = f"the cart {saved}" if cart_id else f"cart {saved}"
    success = f"{'Updated' if cart_id else 'Added'} {what} at {values['store'].strip()}: {money(total, store.settings.currency)}"
    # Keep the date and account to speed up entering another cart.
    form_values = new_cart_form(store.load(), {"date": values["date"], "account": values["account"]})
    response = cart_page(request, store, saved, month, form_values, cart_success=success)
    response.headers["HX-Trigger"] = "ledger-changed"
    return response


@router.get("/cart/{cart_id}/edit")
def edit_cart_form(request: Request, store: StoreDep, cart_id: str, month: str = ""):
    """The Cart tab with its form filled with a cart, to edit it."""
    data = build_carts(store.load(), store.load_carts(), cart_id)
    cart = next((c for c in data["carts"] if c["id"] == cart_id), None)
    if cart is None:
        raise HTTPException(status_code=409, detail=f"No cart {cart_id}: it may have been deleted; reload and try again")
    return cart_page(request, store, cart_id, month, cart_form_of(cart))


@router.post("/cart/{cart_id}/delete")
def delete_cart(request: Request, store: StoreDep, cart_id: str, month: Annotated[str, Form()] = ""):
    try:
        store.delete_cart(cart_id)
    except StaleRowError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        return cart_page(request, store, "", month, cart_error=str(e))
    response = cart_page(request, store, "", month, cart_success=f"Deleted the cart {cart_id} and its movements")
    response.headers["HX-Trigger"] = "ledger-changed"
    return response
