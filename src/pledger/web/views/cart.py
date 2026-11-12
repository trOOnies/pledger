"""cart.py: the Cart tab: the carts, their bags and products, and the form to add or edit one."""

from __future__ import annotations

from pandas import DataFrame

from pledger.models import CreditCardAccount
from pledger.web.store import MAX_INSTALLMENTS, Snapshot, cart_of, row_fingerprint
from pledger.web.store.cart_store import bag_of, cart_accounts, cart_bases, product_name
from pledger.web.store.common import _amount_of, stored_ids
from pledger.web.store.constants import LEDGER_ID
from pledger.web.store.movements import installments_of
from pledger.web.views.forms import bases_by_group, by_group

# The empty product rows of a new bag in the form
NEW_PRODUCTS = 3


def new_cart_form(snapshot: Snapshot, values: dict | None = None) -> dict:
    """The values of the form of a new cart: today, the account and store kept from `values`, one empty bag."""
    values = values or {}
    return {
        "id": "", "date": values.get("date") or snapshot.today.isoformat(), "store": values.get("store", ""),
        "account": values.get("account", ""),
        "bags": [{"base_movement": "", "installments": "", "products": [{"id": "", "name": "", "amount": ""}] * NEW_PRODUCTS}],
    }


def cart_form_of(cart: dict) -> dict:
    """The values of the form to edit a cart (see build_carts)."""
    return {
        "id": cart["id"], "date": cart["date"], "store": cart["store"], "account": cart["account"],
        "bags": [
            {
                "base_movement": b["base"], "installments": str(b["installments"]) if b["installments"] > 1 else "",
                "products": [{"id": p["id"] or "", "name": p["name"], "amount": f"{p['amount']:,.2f}"} for p in b["products"]],
            }
            for b in cart["bags"]
        ],
    }


def carts_of(snapshot: Snapshot, cart_rows: DataFrame) -> tuple[list[dict], list[str], list[str]]:
    """
    Every cart, newest first, with its bags and products from the ledger CSV, the carts of
    the ledger CSV missing from the carts CSV, and the warnings about movements of a cart
    with another date or account than it (e.g. edited by hand).
    """
    carts = {
        row["id"]: {**row, "bags": {}, "total": 0.0, "products": 0}
        for row in cart_rows.to_dict("records")
    }
    ids = stored_ids(snapshot.rows, LEDGER_ID)
    missing, warnings = [], []
    for i, (_, row) in enumerate(snapshot.rows.iterrows()):
        cart_id = cart_of(row)
        if cart_id is None:
            continue
        cart = carts.get(cart_id)
        if cart is None:
            if cart_id not in missing:
                missing.append(cart_id)
            continue
        if row["move_date"] != cart["date"] or row["acc_credit"] != cart["account"]:
            warnings.append(
                f"{ids[i] or 'A movement'} of the cart {cart_id} is of {row['move_date']} crediting {row['acc_credit']}, "
                f"not {cart['date']} crediting {cart['account']}: save the cart again to fix it"
            )
        number = bag_of(row)
        try:
            installments = installments_of(snapshot, row)
        except ValueError:
            installments = 1
        bag = cart["bags"].setdefault(number, {
            "number": number, "base": row["base_movement"], "installments": installments, "products": [], "total": 0.0,
        })
        amount = _amount_of(row["amount"])
        bag["products"].append({
            "index": i, "id": ids[i], "fingerprint": row_fingerprint(row), "name": product_name(cart["store"], row.get("comment", "")),
            "amount": amount, "base": row["base_movement"], "installments": installments,
        })
        bag["total"] += amount
        cart["total"] += amount
        cart["products"] += 1
    found = []
    for cart in carts.values():
        cart["bags"] = [cart["bags"][n] for n in sorted(cart["bags"])]
        found.append(cart)
    found.sort(key=lambda c: (c["date"], int(c["id"]) if str(c["id"]).isdigit() else 0), reverse=True)
    return found, missing, warnings


def build_carts(
    snapshot: Snapshot, cart_rows: DataFrame, cart_id: str = "", month: str = "", form: dict | None = None,
) -> dict:
    """
    The carts of a month (all by default), the one picked (else the newest of them) with
    its bags, and what the form needs: its values (a new cart by default), the accounts a
    cart can be paid from, the loss base movements each of them takes and the credit cards,
    whose carts can have installments.
    """
    carts, missing, warnings = carts_of(snapshot, cart_rows)
    months = sorted({c["date"][:7] for c in carts}, reverse=True)
    shown = [c for c in carts if not month or c["date"].startswith(month)]
    cart = next((c for c in carts if c["id"] == cart_id), None) or (shown[0] if shown else None)
    accounts = cart_accounts(snapshot)
    bases = cart_bases(snapshot)
    return {
        "carts": shown,
        "all_count": len(carts),
        "cart": cart,
        "month": month,
        "months": months,
        "missing": missing,
        "warnings": warnings,
        "stores": sorted({c["store"] for c in carts}, key=str.casefold),
        "form": form or new_cart_form(snapshot),
        "account_groups": by_group(snapshot, accounts),
        "base_groups": bases_by_group(snapshot, bases),
        # The base movements each account takes (see cart_bases), to leave out the others in the form
        "account_bases": {a: cart_bases(snapshot, a) for a in accounts},
        "credit_cards": [a for a in accounts if isinstance(snapshot.accounts[a], CreditCardAccount)],
        "max_installments": MAX_INSTALLMENTS,
    }
