"""Tests for the Cart tab: many movements of the ledger at once, kept together as a cart in bags."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import CREDIT_CARDS_YAML, HTMX

from pledger.web.app import create_app
from pledger.web.store import (
    CART_COLUMN,
    INSTALLMENTS_COLUMN,
    LedgerStore,
    StaleRowError,
    stored_ids,
)
from pledger.web.views import build_carts, build_view


@pytest.fixture
def settings(settings, tmp_path):
    cards = tmp_path / "credit_cards.yaml"
    cards.write_text(CREDIT_CARDS_YAML, encoding="utf-8")
    return replace(settings, carts_path=tmp_path / "carts.csv", credit_cards_path=cards)


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


@pytest.fixture
def store(settings):
    return LedgerStore(settings)


def bag(base, *products, installments=""):
    return {"base_movement": base, "installments": installments, "products": [
        {"id": p[2] if len(p) > 2 else "", "name": p[0], "amount": p[1]} for p in products
    ]}


def cart_rows(store, cart_id):
    rows = store.load().rows
    return rows[rows[CART_COLUMN].str.split("/").str[0] == cart_id]


def view(store, cart_id=""):
    return build_carts(store.load(), store.load_carts(), cart_id)


class TestSaveCart:
    def test_adds_a_movement_per_product(self, store, settings):
        cart_id = store.save_cart("", "2026-01-10", "Coto", "Ca", [
            bag("Food", ("Milk", "10"), ("Bread", "5.5")),
            bag("Food", ("Apples", "3")),
        ])
        assert cart_id == "1"
        rows = cart_rows(store, "1")
        assert list(rows.comment) == ["Coto: Milk", "Coto: Bread", "Coto: Apples"]
        assert list(rows[CART_COLUMN]) == ["1/1", "1/1", "1/2"]
        assert set(rows.acc_credit) == {"Ca"} and set(rows.acc_debit) == {"Eq"} and set(rows.move_date) == {"2026-01-10"}
        assert list(store.load_carts().store) == ["Coto"]
        # The other lines of the ledger CSV are left as they were
        assert settings.ledger_path.read_text().splitlines()[1].startswith('2026-01-01;Salary;Bk;"1,000.50";Eq;')

    def test_the_cart_view_groups_them_in_bags(self, store):
        store.save_cart("", "2026-01-10", "Coto", "Ca", [bag("Food", ("Milk", "10"), ("", "2")), bag("Food", ("Apples", "3"))])
        cart = view(store)["cart"]
        assert cart["total"] == 15 and cart["products"] == 3
        assert [(b["number"], b["total"], [p["name"] for p in b["products"]]) for b in cart["bags"]] == [
            (1, 12, ["Milk", ""]), (2, 3, ["Apples"]),
        ]

    def test_leaves_out_empty_products_and_bags(self, store):
        store.save_cart("", "2026-01-10", "Coto", "Ca", [bag("Food", ("Milk", "10"), ("", "")), bag("", ("", ""))])
        assert len(cart_rows(store, "1")) == 1

    def test_installments_of_a_credit_card(self, store):
        store.save_cart("", "2026-01-10", "Shop", "Vi", [bag("Food", ("TV", "1200"), installments="6"), bag("Food", ("Cable", "20"))])
        rows = cart_rows(store, "1")
        assert list(rows[INSTALLMENTS_COLUMN]) == ["6", ""]
        assert [b["installments"] for b in view(store)["cart"]["bags"]] == [6, 1]

    @pytest.mark.parametrize(("account", "bags", "error"), [
        ("Ca", [bag("Food", ("Milk", "10"), installments="3")], "Only a movement crediting a credit card"),
        ("Bk", [bag("Food", ("Milk", "10"))], "base currency"),
        ("Fi", [bag("Food", ("Milk", "10"))], "can't be paid from Fi"),
        ("Ca", [bag("Salary", ("Milk", "10"))], "is a loss of the Ledger tab"),
        ("Ca", [bag("Card fee", ("Fee", "10"))], "can't be paid from Ca"),
        ("Ca", [bag("Food", ("Milk", ""))], r"amount of Milk \(bag 1\) is required"),
        ("Ca", [bag("Food", ("Milk", "0"))], "must be more than 0"),
        ("Ca", [bag("Food", ("", ""))], "at least one product"),
        ("", [bag("Food", ("Milk", "10"))], "Pick the account"),
    ])
    def test_invalid(self, store, settings, account, bags, error):
        before = settings.ledger_path.read_bytes()
        with pytest.raises(ValueError, match=error):
            store.save_cart("", "2026-01-10", "Coto", account, bags)
        assert settings.ledger_path.read_bytes() == before and not settings.carts_path.exists()

    def test_the_store_is_required(self, store):
        with pytest.raises(ValueError, match="store is required"):
            store.save_cart("", "2026-01-10", " ", "Ca", [bag("Food", ("Milk", "10"))])


class TestEditCart:
    def test_keeps_the_ids_of_the_products_kept(self, store):
        store.save_cart("", "2026-01-10", "Coto", "Ca", [bag("Food", ("Milk", "10"), ("Bread", "5"))])
        milk, bread = stored_ids(cart_rows(store, "1"), "L")
        store.save_cart("1", "2026-01-11", "Dia", "Vi", [
            bag("Food", ("Eggs", "4")),
            bag("Food", ("Milk", "12", milk), installments="2"),
        ])
        rows = cart_rows(store, "1")
        ids = stored_ids(rows, "L")
        assert ids[0] == milk and bread not in ids and len(ids) == 2
        assert list(rows.comment) == ["Dia: Milk", "Dia: Eggs"]
        assert list(rows[CART_COLUMN]) == ["1/2", "1/1"] and list(rows.amount) == ["12.00", "4.00"]
        assert set(rows.move_date) == {"2026-01-11"} and set(rows.acc_credit) == {"Vi"}
        assert store.load_carts().to_dict("records") == [{"id": "1", "date": "2026-01-11", "store": "Dia", "account": "Vi"}]

    def test_an_id_of_another_cart_is_stale(self, store):
        store.save_cart("", "2026-01-10", "Coto", "Ca", [bag("Food", ("Milk", "10"))])
        store.save_cart("", "2026-01-10", "Dia", "Ca", [bag("Food", ("Eggs", "4"))])
        (other,) = stored_ids(cart_rows(store, "2"), "L")
        with pytest.raises(StaleRowError):
            store.save_cart("1", "2026-01-10", "Coto", "Ca", [bag("Food", ("Eggs", "4", other))])

    def test_delete_removes_its_movements(self, store, settings):
        store.save_cart("", "2026-01-10", "Coto", "Ca", [bag("Food", ("Milk", "10"))])
        store.save_cart("", "2026-01-10", "Dia", "Ca", [bag("Food", ("Eggs", "4"))])
        store.delete_cart("1")
        assert cart_rows(store, "1").empty and len(cart_rows(store, "2")) == 1
        assert list(store.load_carts().id) == ["2"]


class TestLedgerTab:
    def test_its_movements_link_to_the_cart_tab_and_are_not_editable(self, store):
        store.save_cart("", "2026-01-10", "Coto", "Ca", [bag("Food", ("Milk", "10"))])
        (movement,) = [m for m in build_view(store.load())["movements"] if m["comment"] == "Coto: Milk"]
        assert movement["source"] == "cart" and not movement["editable"]
        assert movement["link"] == f"/cart?cart=1#movement-{movement['id']}"

    def test_they_cant_be_edited_or_deleted_there(self, store):
        store.save_cart("", "2026-01-10", "Coto", "Ca", [bag("Food", ("Milk", "10"))])
        index = len(store.load().rows) - 1
        with pytest.raises(ValueError, match="Cart tab"):
            store.delete(index)
        with pytest.raises(ValueError, match="Cart tab"):
            store.edit(index, None, "2026-01-10", "Food", "Eq", "Ca", "11")

    def test_other_movements_are_left_alone(self, store):
        store.save_cart("", "2026-01-10", "Coto", "Ca", [bag("Food", ("Milk", "10"))])
        store.add("2026-01-12", "Food", "Eq", "Ca", "1", "by hand")
        store.delete(len(store.load().rows) - 1)


class TestPages:
    def test_add_from_the_form(self, client, store):
        r = client.post("/cart/carts", headers=HTMX, data={
            "date": "2026-01-10", "store": "Coto", "account": "Ca",
            "bags-1-base_movement": "Food", "bags-1-name": ["Milk", "Bread"], "bags-1-amount": ["10", "5"],
            "bags-1-id": ["", ""],
            "bags-2-base_movement": "Food", "bags-2-name": ["Apples"], "bags-2-amount": ["3"], "bags-2-id": [""],
        })
        assert r.status_code == 200 and "Added cart 1 at Coto: $18.00" in r.text
        assert list(cart_rows(store, "1")[CART_COLUMN]) == ["1/1", "1/1", "1/2"]

    def test_an_error_keeps_what_was_entered(self, client):
        r = client.post("/cart/carts", headers=HTMX, data={
            "date": "2026-01-10", "store": "Coto", "account": "Ca",
            "bags-1-base_movement": "Salary", "bags-1-name": ["Milk"], "bags-1-amount": ["10"], "bags-1-id": [""],
        })
        assert "is a loss of the Ledger tab" in r.text and 'value="Milk"' in r.text

    def test_page_edit_and_delete(self, client, store):
        store.save_cart("", "2026-01-10", "Coto", "Ca", [bag("Food", ("Milk", "10"))])
        r = client.get("/cart")
        assert r.status_code == 200 and "Cart 1" in r.text and "Coto" in r.text
        r = client.get("/cart/1/edit", headers=HTMX)
        assert "Edit cart" in r.text and 'value="Milk"' in r.text
        r = client.post("/cart/1/delete", headers=HTMX)
        assert "Deleted the cart 1" in r.text and store.load_carts().empty

    def test_the_tab_is_in_the_menu(self, client):
        assert 'href="/cart"' in client.get("/").text
