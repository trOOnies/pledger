"""cart_store.py: the carts of the Cart tab, whose products are movements of the ledger CSV."""

from __future__ import annotations

import re
from dataclasses import replace

from pandas import DataFrame, concat

from pledger.enums import AccountType, LedgerSide, MovementType
from pledger.models import DebtAccount, InvestmentAccount, is_foreign
from pledger.web.store.common import (
    StaleRowError,
    _amount_of,
    _fit_row,
    _parse_date,
    _with_row,
    stored_ids,
)
from pledger.web.store.constants import (
    CART_COLUMN,
    CART_COLUMNS,
    INSTALLMENTS_COLUMN,
    LEDGER_ID,
)
from pledger.web.store.movement_store import MovementStore
from pledger.web.store.movements import _installments_text, _movement_row, cart_of
from pledger.web.store.plan_store import PlanStore
from pledger.web.store.snapshot import Snapshot


def _clean(text: str) -> str:
    """A text of a CSV cell on one line, without the separator."""
    return re.sub(r"\s*[;\r\n]+\s*", ", ", str(text)).strip()


def product_comment(store: str, name: str) -> str:
    """The comment of the movement of a product of a cart: the store, then the product."""
    return f"{store}: {name}" if name else store


def product_name(store: str, comment: str) -> str:
    """The product of the comment of a movement of a cart (see product_comment)."""
    comment = str(comment or "")
    if comment == store:
        return ""
    return comment.removeprefix(f"{store}: ")


def cart_positions(snapshot: Snapshot, cart_id: str) -> list[int]:
    """The positions in the ledger CSV of the movements of a cart, in their order there."""
    rows = snapshot.rows
    if CART_COLUMN not in rows.columns:
        return []
    return [i for i, (_, row) in enumerate(rows.iterrows()) if cart_of(row) == cart_id]


def bag_of(row) -> int:
    """The number of the bag of a movement of a cart (see CART_COLUMN), 1 if it has none."""
    _, _, bag = str(row.get(CART_COLUMN, "")).partition("/")
    return int(bag) if bag.strip().isdigit() else 1


class CartStore(MovementStore):
    """
    Adds, edits and deletes the carts: a purchase at a store on a date, paid from an
    account, in the carts CSV, whose products are movements of the ledger CSV, in bags.

    Each bag is a group of products of the same loss base movement and, paid with a
    credit card, the same installments: each product is a movement of it from equity,
    crediting the account of the cart, its comment naming the store then the product, and
    its cart and bag in its CART_COLUMN. Two bags may share both: they only organize the cart.
    """

    def load_carts(self) -> DataFrame:
        return PlanStore._plan_rows(self.settings.carts_path, CART_COLUMNS, "carts")

    def save_cart(
        self, cart_id: str, move_date: str, store: str, account: str, bags: list[dict],
    ) -> str:
        """
        Add a cart (with no `cart_id`) or replace one, with its movements, and give its
        id. Each bag has its `base_movement`, its `installments` (1 if empty) and its
        `products`, each with its `name` (optional), `amount` and, if it is already a movement
        of the cart, its `id`: that movement is replaced in its line, keeping its id, while
        the products without one are appended to the ledger CSV, and the movements of the cart
        left out are deleted. The empty products and bags are left out. All of it or nothing.
        """
        snapshot = self.load()
        carts = self.load_carts()
        cart_id = str(cart_id).strip()
        if cart_id and cart_id not in set(carts["id"]):
            raise StaleRowError(f"No cart {cart_id}: it may have been deleted; reload and try again")
        if _parse_date(move_date, "date") is None:
            raise ValueError("The date is required")
        store = _clean(store)
        if not store:
            raise ValueError("The store is required")
        _check_cart_account(snapshot, account)
        new_id = cart_id or str(max((int(i) for i in carts["id"] if str(i).isdigit()), default=0) + 1)

        rows = snapshot.rows
        positions = cart_positions(snapshot, cart_id) if cart_id else []
        ids = stored_ids(rows, LEDGER_ID)
        own = {ids[i]: i for i in positions if ids[i]}
        replaced: dict[int, dict] = {}
        appended: list[dict] = []
        number = 0
        for bag in bags:
            products = [p for p in bag.get("products", []) if str(p.get("name", "")).strip() or str(p.get("amount", "")).strip()]
            if not products:
                continue
            number += 1
            base_name = str(bag.get("base_movement", "")).strip()
            base = _check_cart_base(snapshot, base_name, account, number)
            installments = _installments_text(snapshot, bag.get("installments", ""), account)
            for product in products:
                name = _clean(product.get("name", ""))
                if not str(product.get("amount", "")).strip():
                    raise ValueError(f"The amount of {name or 'a product'} (bag {number}) is required")
                _, row = _movement_row(
                    snapshot, move_date, base_name, base.acc_debit.name, account, product["amount"],
                    product_comment(store, name), False, "", "", False, False,
                )
                if _amount_of(row["amount"]) == 0:
                    raise ValueError(f"The amount of {name or 'a product'} (bag {number}) must be more than 0")
                row[INSTALLMENTS_COLUMN] = installments
                row[CART_COLUMN] = f"{new_id}/{number}"
                own_id = str(product.get("id", "")).strip()
                if own_id:
                    if own_id not in own or own[own_id] in replaced:
                        raise StaleRowError(f"The cart changed since this page was loaded ({own_id}); reload and try again")
                    replaced[own[own_id]] = row
                else:
                    appended.append(row)
        if not replaced and not appended:
            raise ValueError("Add at least one product, with its amount")
        deleted = [i for i in positions if i not in replaced]

        new_columns = [
            c for c in (INSTALLMENTS_COLUMN, CART_COLUMN)
            if c not in rows.columns and any(r.get(c) for r in [*replaced.values(), *appended])
        ]
        frame = rows.assign(**{c: "" for c in new_columns}) if new_columns else rows
        changed = frame
        for i, row in replaced.items():
            changed = _with_row(changed, i, row)
        changed = changed.drop(index=changed.index[deleted])
        fitted = [_fit_row(row, frame.columns, "ledger") for row in appended]
        if fitted:
            changed = concat([changed, DataFrame(fitted)], ignore_index=True)
        replace(snapshot, rows=changed).ledger().check_accounts()

        path = self.settings.ledger_path
        for column in new_columns:
            self._add_column(path, len(rows), column)
        csv_file = self._appended(path, LEDGER_ID, fitted, list(frame.columns))
        header = csv_file.lines[0].removeprefix("﻿")
        for i, row in replaced.items():
            line = csv_file.row_lines[i]
            csv_file.lines[line] = self._replaced_line(header, csv_file.lines[line], row)
        for i in sorted(deleted, reverse=True):
            del csv_file.lines[csv_file.row_lines[i]]
        self._write_file(csv_file)

        cart = {"id": new_id, "date": move_date, "store": store, "account": account}
        carts = carts[carts["id"] != new_id]
        PlanStore._write_plan(self.settings.carts_path, PlanStore._with_row(carts, cart), "carts")
        return new_id

    def delete_cart(self, cart_id: str) -> None:
        """Delete a cart and its movements of the ledger CSV, all of them or none."""
        snapshot = self.load()
        carts = self.load_carts()
        cart_id = str(cart_id).strip()
        positions = cart_positions(snapshot, cart_id)
        if cart_id not in set(carts["id"]) and not positions:
            raise StaleRowError(f"No cart {cart_id}: it may have been deleted; reload and try again")
        if positions:
            rows = snapshot.rows
            replace(snapshot, rows=rows.drop(index=rows.index[positions])).ledger().check_accounts()
            csv_file = self._read_file(len(rows), self.settings.ledger_path)
            for i in reversed(positions):
                del csv_file.lines[csv_file.row_lines[i]]
            self._write_file(csv_file)
        PlanStore._write_plan(self.settings.carts_path, carts[carts["id"] != cart_id], "carts")


def cart_accounts(snapshot: Snapshot) -> list[str]:
    """
    The accounts a cart can be paid from: the asset and liability accounts in the base
    currency but the internal, investment and debt ones (credit cards included).
    """
    return [
        a.name for a in snapshot.accounts.values()
        if a.acc_type != AccountType.EQUITY and not a.internal and not is_foreign(a)
        and not isinstance(a, InvestmentAccount | DebtAccount)
    ]


def cart_bases(snapshot: Snapshot, account: str = "") -> list[str]:
    """
    The base movements of the bags of a cart: the loss ones of the Ledger tab, those whose
    credit side takes this account if any.
    """
    found = []
    for name, base in snapshot.ledger_base_movements().items():
        if base.move_type != MovementType.LOSS:
            continue
        if account and not _takes(snapshot, base, account):
            continue
        found.append(name)
    return found


def _takes(snapshot: Snapshot, base, account: str) -> bool:
    """Whether the credit side of a base movement takes this account."""
    if base.acc_credit is not None:
        return base.acc_credit.name == account
    return account in {a.name for a in snapshot.side_accounts(base, LedgerSide.CREDIT)}


def _check_cart_account(snapshot: Snapshot, account: str) -> None:
    if not account:
        raise ValueError("Pick the account the cart is paid from")
    found = snapshot.accounts.get(account)
    if found is None:
        raise ValueError(f"Unknown account: {account!r}")
    if found.acc_type != AccountType.EQUITY and is_foreign(found):
        raise ValueError(f"A cart is paid from an account in the base currency, not {account} ({found.currency.name})")
    if account not in cart_accounts(snapshot):
        raise ValueError(f"A cart can't be paid from {account}: pick an asset or liability account of your own")


def _check_cart_base(snapshot: Snapshot, base_name: str, account: str, number: int):
    """The base movement of a bag: a loss of the Ledger tab, which takes the account of the cart."""
    if not base_name:
        raise ValueError(f"Pick the base movement of bag {number}")
    base = snapshot.ledger_base_movements().get(base_name)
    if base is None or base.move_type != MovementType.LOSS:
        raise ValueError(f"The base movement of a bag is a loss of the Ledger tab, not {base_name!r}")
    if not _takes(snapshot, base, account):
        raise ValueError(f"The base movement {base_name!r} (bag {number}) can't be paid from {account}")
    return base
