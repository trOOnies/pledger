"""Tests for the Split tab: the tab of a get-together split among its people, and who pays whom."""

import re
from dataclasses import replace
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from helpers import DEBT_YAML, HTMX, with_internal

from pledger.enums import CreditorKind
from pledger.web.app import create_app
from pledger.web.store import LedgerStore
from pledger.web.views.split import settle, split


def item(name, price, *shares):
    return {"name": name, "price": price, "shares": list(shares)}


class TestSplit:
    def test_evenly_among_who_fronts_each_item(self):
        result = split(
            ["Ana", "Leo"],
            [item("Pizza", "30", "on", "on", "on"), item("Beer", "10", "", "on", "on"), item("Wine", "10.01", "on")],
            ["50.01", "", ""],
        )
        assert [(p["name"], p["owes"], p["paid"], p["balance"]) for p in result["people"]] == [
            ("You", 2001, 5001, 3000), ("Ana", 1500, 0, -1500), ("Leo", 1500, 0, -1500),
        ]
        assert result["transfers"] == [
            {"from": "Ana", "to": "You", "cents": 1500}, {"from": "Leo", "to": "You", "cents": 1500},
        ]
        assert result["total"] == 5001

    def test_the_cents_left_go_to_the_first_ones(self):
        result = split(["Ana", "Leo"], [item("Taxi", "10", "on", "on", "on")], ["", "10", ""])
        assert [p["owes"] for p in result["people"]] == [334, 333, 333]
        assert [p["owes"] for p in split(["Ana"], [item("X", "0.01", "on", "on")], ["", "0.01"])["people"]] == [1, 0]

    def test_percentages(self):
        result = split(["Ana"], [item("X", "100", "25", "75"), item("Y", "1", "33.33", "66.67")], ["0", "101"], True)
        assert [p["owes"] for p in result["people"]] == [2533, 7567]
        assert result["transfers"] == [{"from": "You", "to": "Ana", "cents": 2533}]

    def test_everyone_even(self):
        assert split(["Ana"], [item("X", "10", "on", "on")], ["5", "5"])["transfers"] == []

    def test_empty_items_are_left_out(self):
        assert len(split(["Ana"], [item("", ""), item("X", "1", "on")], ["1"])["items"]) == 1

    @pytest.mark.parametrize(("others", "items", "paid", "percentages", "error"), [
        ([], [item("X", "1", "on")], ["1"], False, "Add at least one other person"),
        ([f"P{i}" for i in range(8)], [item("X", "1", "on")], ["1"], False, "At most 7 other people"),
        (["Ana", " "], [item("X", "1", "on")], ["1"], False, "Person 2 needs a name"),
        (["Ana", "ana"], [item("X", "1", "on")], ["1"], False, "'ana' is twice"),
        (["you"], [item("X", "1", "on")], ["1"], False, "'You' is you already"),
        (["Ana"], [], ["1"], False, "Add at least one item"),
        (["Ana"], [item("X", "1")], ["1"], False, "Check who fronts 'X'"),
        (["Ana"], [item("", "1", "on")], ["1"], False, "The name of item 1 is required"),
        (["Ana"], [item("X", "", "on")], ["1"], False, "The price of 'X' is required"),
        (["Ana"], [item("X", "-1", "on")], ["1"], False, "cannot be negative"),
        (["Ana"], [item("X", "1", "on")], ["-1", "2"], False, "cannot be negative"),
        (["Ana"], [item("X", "1", "50", "40")], ["1"], True, "add up to 90%, not 100%"),
        (["Ana"], [item("X", "1", "120", "-20")], ["1"], True, "cannot be negative"),
        (["Ana"], [item("X", "1", "0", "")], ["1"], True, "Give a percentage of 'X' to someone"),
        (["Ana"], [item("X", "1", "abc", "")], ["1"], True, "Invalid percentage of 'X'"),
        (["Ana"], [item("X", "10", "on")], ["4", "5"], False, "The tab is 10.00, but everyone put in 9.00: add 1.00"),
    ])
    def test_errors(self, others, items, paid, percentages, error):
        with pytest.raises(ValueError, match=re.escape(error)):
            split(others, items, paid, percentages)

    def test_settle(self):
        # The one owing the most pays the one owed the most first
        assert settle(["You", "A", "B", "C"], [-10, -30, 25, 15]) == [
            {"from": "A", "to": "B", "cents": 25}, {"from": "You", "to": "C", "cents": 10},
            {"from": "A", "to": "C", "cents": 5},
        ]


class TestSplitTab:
    @pytest.fixture
    def client(self, settings):
        return TestClient(create_app(settings))

    def test_page(self, client):
        r = client.get("/split")
        assert r.status_code == 200 and 'href="/split" aria-current="page"' in r.text
        form = r.text.split('id="split-form"')[1].split("</form>")[0]
        # The user and one more, and one item to fill in
        assert 'name="others"' in form and 'name="items-1-share-0"' in form and 'name="items-1-share-1"' in form
        assert 'name="items-1-share-2"' not in form.split("<template")[0]
        assert form.split("<template")[0].count('name="paid"') == 2
        assert "Who pays whom" not in r.text
        # One table: the names in the headers of their columns, with + adding one, and what each put in its last row
        table = form.split('<table class="split-table">')[1].split("</table>")[0]
        head = table.split("</thead>")[0]
        assert 'name="others"' in head and "data-add-person" in head and "data-remove-person" in head
        assert table.split("<tfoot>")[1].count('name="paid"') == 2 and "<fieldset" not in form

    def test_split(self, client, settings):
        data = {
            "others": ["Ana", "Leo"], "items-1-name": "Pizza", "items-1-price": "30",
            "items-1-share-0": "on", "items-1-share-1": "on", "items-1-share-2": "on",
            "items-2-name": "Beer", "items-2-price": "9", "items-2-share-1": "on", "items-2-share-2": "on",
            "paid": ["", "39", ""],
        }
        r = client.post("/split", data=data, headers=HTMX)
        assert "<h3>Who pays whom" in r.text and "<html" not in r.text
        transfers = re.sub(r"\s+", " ", r.text.split('class="split-transfers">')[1].split("</ul>")[0])
        assert "<strong>You</strong> pay <strong>Ana</strong> <span class=\"money\">$10.00</span>" in transfers
        assert "<strong>Leo</strong> pays <strong>Ana</strong> <span class=\"money\">$14.50</span>" in transfers
        # The form keeps what was entered
        assert 'value="Pizza"' in r.text and 'value="Leo"' in r.text and 'value="39"' in r.text
        assert not settings.ledger_path.read_text().count("Pizza")  # nothing is saved

    def test_percentages_and_errors(self, client):
        data = {
            "others": ["Ana"], "percentages": "true", "items-1-name": "X", "items-1-price": "10",
            "items-1-share-0": "50", "items-1-share-1": "40", "paid": ["10", ""],
        }
        r = client.post("/split", data=data, headers=HTMX)
        assert 'class="msg error"' in r.text and "add up to 90%" in r.text and "Who pays whom" not in r.text
        assert 'value="40"' in r.text and 'name="percentages" value="true" checked' in r.text
        r = client.post("/split", data={**data, "items-1-share-1": "50"}, headers=HTMX)
        assert "Ana</strong> pays <strong>You</strong>" in re.sub(r"\s+", " ", r.text)

    def test_api(self, client):
        body = {"others": ["Ana"], "items": [{"name": "X", "price": 10, "shares": [1, 1]}], "paid": [10, 0]}
        r = client.post("/api/split", json=body)
        assert r.status_code == 200 and r.json()["transfers"] == [{"from": "Ana", "to": "You", "cents": 500}]
        r = client.post("/api/split", json={**body, "percentages": True, "items": [{"name": "X", "price": 10, "shares": [20, 80]}]})
        assert r.json()["transfers"] == [{"from": "Ana", "to": "You", "cents": 800}]
        assert client.post("/api/split", json={**body, "others": []}).status_code == 422


class TestSplitMovements:
    """Steps 2 and 3: the movements of the user's part of a split, and the people they add."""

    @pytest.fixture
    def settings(self, settings, tmp_path):
        path = tmp_path / "creditors.yaml"
        path.write_text("people:\n  Ana: 0\n", encoding="utf-8")
        with_internal(settings.base_movements_path, transfers=DEBT_YAML)
        return replace(settings, creditors_path=path)

    @staticmethod
    def plain(plan):
        return [(m["base_movement"], m["acc_debit"], m["acc_credit"], m["cents"], m["comment"]) for m in plan["movements"]]

    def test_owed_and_partly_settled(self, settings):
        # You paid 30 for a pizza of three: Ana and Leo owe you 10 each
        result = split(["ana", "Leo"], [item("Pizza", "30", "on", "on", "on") | {"base": "Food"}], ["30", "", ""])
        store = LedgerStore(settings)
        plan = store.plan_split(result, "2026-02-01", "Friday", "Ca", {"ana": ("4", "Ca"), "Leo": ("", "")})
        assert self.plain(plan) == [
            ("Food", "Eq", "Ca", 1000, "Friday - Pizza"),
            ("Loan - Given", "Lo", "Ca", 1000, "Ana: Friday"),  # as in the People tab
            ("Loan - Given", "Lo", "Ca", 1000, "Leo: Friday"),
            ("Loan - Collected", "Ca", "Lo", 400, "Ana: Friday"),
        ]
        assert plan["new_people"] == ["Leo"] and "Leo" not in settings.creditors_path.read_text()  # not yet
        before = settings.ledger_path.read_text()
        created = store.create_split(result, "2026-02-01", "Friday", "Ca", {"ana": ("4", "Ca")})
        assert created["new_people"] == ["Leo"] and "Leo: 0" in settings.creditors_path.read_text()
        assert len(settings.ledger_path.read_text().splitlines()) == len(before.splitlines()) + 4
        lo = next(a for a in store.load().debt_accounts(kind=CreditorKind.PERSON) if a.name == "Lo")
        assert lo.creditors["Ana"].amount == pytest.approx(-6) and lo.creditors["Leo"].amount == pytest.approx(-10)

    def test_owing_without_paying(self, settings):
        # Ana paid it all: you owe her your share, paid out of De, and pay back 5 of it
        result = split(["Ana"], [item("Pizza", "20", "on", "on") | {"base": "Food"}], ["", "20"])
        plan = LedgerStore(settings).plan_split(result, "2026-02-01", "", "", {"Ana": ("5", "Ca")})
        assert self.plain(plan) == [("Food", "Eq", "De", 1000, "Ana: Pizza"), ("Debt - Paid", "De", "Ca", 500, "Ana: Split")]
        LedgerStore(settings).create_split(result, "2026-02-01", "", "", {"Ana": ("5", "Ca")})
        de = LedgerStore(settings).load().debt_accounts(kind=CreditorKind.PERSON)[0]
        assert de.creditors["Ana"].amount == pytest.approx(5)

    def test_an_item_out_of_two_sources(self, settings):
        result = split(
            ["Ana"], [item("A", "10", "on", "on") | {"base": "Food"}, item("B", "20", "on", "on") | {"base": "Food"}],
            ["8", "22"],
        )
        assert self.plain(LedgerStore(settings).plan_split(result, "2026-02-01", "", "Ca")) == [
            ("Food", "Eq", "Ca", 500, "A"), ("Food", "Eq", "Ca", 300, "B"), ("Food", "Eq", "De", 700, "Ana: B"),
        ]

    def test_checked_before_anything_is_written(self, settings):
        # Cash can't go below 0 (it has 159.75): Process all says so already
        settings.accounts_path.write_text(
            settings.accounts_path.read_text().replace(
                "    is_liquid: true\n    initial_amount: 0.0\n", "    is_liquid: true\n    initial_amount: 0.0\n    checks: [net_positive]\n", 1,
            )
        )
        result = split(["Leo"], [item("X", "2000", "on", "on") | {"base": "Food"}], ["2000", ""])
        with pytest.raises(ValueError, match="Ca"):
            LedgerStore(settings).plan_split(result, "2026-02-01", "", "Ca")

    def test_rolls_back_the_new_people(self, settings, monkeypatch):
        result = split(["Leo"], [item("X", "20", "on", "on") | {"base": "Food"}], ["20", ""])
        before, creditors = settings.ledger_path.read_text(), settings.creditors_path.read_text()
        store = LedgerStore(settings)

        def fail(*args):
            raise ValueError("the disk is full")

        monkeypatch.setattr(store, "_append_rows", fail)
        with pytest.raises(ValueError, match="the disk is full"):
            store.create_split(result, "2026-02-01", "", "Ca")
        assert settings.ledger_path.read_text() == before and settings.creditors_path.read_text() == creditors

    @pytest.mark.parametrize(("base", "paid_from", "settle", "error"), [
        ("Food", "", {}, "Pick the account you paid from"),
        ("Food", "Bk", {}, "'Bk' can't be the account you paid from"),
        ("", "Ca", {}, "Pick the base movement of 'X': you have a share in it"),
        ("Gift", "Ca", {}, "'Gift' can't be the base movement of 'X'"),
        ("Food", "Ca", {"Ana": ("6", "Ca")}, "Ana can't settle more than the 5.00 they owe you"),
        ("Food", "Ca", {"Ana": ("-1", "Ca")}, "cannot be negative"),
        ("Food", "Ca", {"Ana": ("1", "")}, "Pick the account Ana pays you into"),
    ])
    def test_errors(self, settings, base, paid_from, settle, error):
        result = split(["Ana"], [item("X", "10", "on", "on") | {"base": base}], ["10", ""])
        with pytest.raises(ValueError, match=re.escape(error)):
            LedgerStore(settings).plan_split(result, "2026-02-01", "", paid_from, settle)

    def test_not_a_service(self, settings):
        settings.creditors_path.write_text("people:\n  Ana: 0\nservices:\n  Gym: 0\n")
        result = split(["gym"], [item("X", "10", "on", "on") | {"base": "Food"}], ["10", ""])
        with pytest.raises(ValueError, match="'gym' is a service of pledger, not a person"):
            LedgerStore(settings).plan_split(result, "2026-02-01", "", "Ca")


class TestSplitSteps:
    """The form through its steps: split, process all, return to settling and create the movements."""

    @pytest.fixture
    def settings(self, settings, tmp_path):
        path = tmp_path / "creditors.yaml"
        path.write_text("people:\n  Ana: 0\n", encoding="utf-8")
        with_internal(settings.base_movements_path, transfers=DEBT_YAML)
        return replace(settings, creditors_path=path)

    @pytest.fixture
    def client(self, settings):
        return TestClient(create_app(settings))

    DATA: ClassVar[dict] = {
        "others": ["ana", "Leo"], "items-1-name": "Pizza", "items-1-price": "30", "items-1-base": "Food",
        "items-1-share-0": "on", "items-1-share-1": "on", "items-1-share-2": "on", "paid": ["30", "", ""],
    }

    def test_steps(self, client, settings):
        r = client.get("/split")
        assert "Step 1: What was meant vs What was paid" in r.text and "Step 2" not in r.text
        assert '<option value="Ana">' in r.text  # the people of pledger, for the names
        assert '<option >Food</option>' in r.text and 'name="items-1-base"' in r.text
        # Split: Step 2, who is in People and who would be added, and what you paid from
        r = client.post("/split", data={**self.DATA, "action": "split"}, headers=HTMX)
        step2 = r.text.split("data-step2")[1].split("</section>")[0]
        assert "Step 2: Settling differences" in step2 and 'name="paid_from"' in step2
        assert step2.count('name="settle-') == 3 * 2  # who, amount and account of Ana and Leo
        assert "In People" in step2 and "New: added to People" in step2 and 'value="0.00"' in step2
        assert "Step 3" not in r.text
        settle = {"date": "2026-02-01", "description": "Friday", "paid_from": "Ca",
                  "settle-1-who": "ana", "settle-1-amount": "4", "settle-1-account": "Ca",
                  "settle-2-who": "Leo", "settle-2-amount": "0.00", "settle-2-account": ""}
        # Process all: Step 3, with Steps 1 and 2 kept but inert
        before = settings.ledger_path.read_text()
        r = client.post("/split", data={**self.DATA, **settle, "action": "process"}, headers=HTMX)
        assert "Step 3: Reflect with movements" in r.text and "data-step1 inert" in r.text and "data-step2 inert" in r.text
        step3 = re.sub(r"\s+", " ", r.text.split("data-step3")[1].split("</section>")[0])
        assert step3.count("<tr>") == 1 + 4 and "Loan - Collected" in step3 and "Ana: Friday" in step3
        assert "<strong>Leo</strong>" in step3 and 'value="create"' in step3 and 'value="settle"' in step3
        assert settings.ledger_path.read_text() == before and "Leo" not in settings.creditors_path.read_text()
        # Return to Settling: Step 2 again, with what was entered
        r = client.post("/split", data={**self.DATA, **settle, "action": "settle"}, headers=HTMX)
        assert "Step 3" not in r.text and 'value="Friday"' in r.text and "data-step2 inert" not in r.text
        # Create movements: in the ledger, Leo in People, and a new split
        r = client.post("/split", data={**self.DATA, **settle, "action": "create"}, headers=HTMX)
        assert "Created 4 movements" in r.text and "added Leo to" in r.text and 'value="Pizza"' not in r.text
        assert len(settings.ledger_path.read_text().splitlines()) == len(before.splitlines()) + 4
        assert "Leo: 0" in settings.creditors_path.read_text()

    def test_errors_of_step2(self, client, settings):
        r = client.post("/split", data={**self.DATA, "action": "process", "date": "2026-02-01"}, headers=HTMX)
        assert "Pick the account you paid from" in r.text and "Step 3" not in r.text
        r = client.post("/split", data={**self.DATA, "items-1-base": "", "action": "create", "paid_from": "Ca"}, headers=HTMX)
        assert "Pick the base movement of &#39;Pizza&#39;" in r.text
        assert "Leo" not in settings.creditors_path.read_text()
