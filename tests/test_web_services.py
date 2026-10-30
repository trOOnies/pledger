"""Tests for the Services tab."""

import datetime as dt
import html
import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import HTMX, SERVICES_HEADER, card_labels

from pledger.web.app import create_app
from pledger.web.store import (
    LedgerDataError,
    LedgerStore,
)


class TestServices:
    """The monthly values of the services of De, a column each, owed to them week by week."""

    @pytest.fixture
    def settings(self, settings, tmp_path, monkeypatch):
        monkeypatch.setattr("pledger.web.store.common.today", lambda: dt.date(2026, 2, 15))
        creditors = tmp_path / "creditors.yaml"
        creditors.write_text(
            "De:\n  people:\n    Dad: 0\n  services:\n"
            "    Rent:\n      amount: 0\n      base_movement: Food\n"
            "    Expenses:\n      base_movement: Food\n"
            "    Gas:\n      amount: 5\n      base_movement: Food\n"
            "    Water: 0\n",
            encoding="utf-8",
        )
        services = tmp_path / "services.csv"
        services.write_text(SERVICES_HEADER + "2025-12;100;;\n2026-01;100;30;50\n2026-02;;30;\n", encoding="utf-8")
        return replace(settings, creditors_path=creditors, services_path=services)

    @pytest.fixture
    def client(self, settings):
        return TestClient(create_app(settings))

    @staticmethod
    def owed(settings):
        snapshot = LedgerStore(settings).load()
        return [(r.id, r.move_date, r.base_movement, r.acc_debit, r.acc_credit, r.amount, r.comment)
                for r in snapshot.service_rows.itertuples()]

    def test_services_are_owed_up_to_today(self, settings):
        assert self.owed(settings) == [
            # Week 1 of 2026-01 starts on 2025-12-29, before the ledger, like 2025-12
            ("S-Rent-2026-01-2", "2026-01-05", "Food", "Eq", "De", "20.00", "Rent: week 2/5 of 2026-01"),
            ("S-Rent-2026-01-3", "2026-01-12", "Food", "Eq", "De", "20.00", "Rent: week 3/5 of 2026-01"),
            ("S-Rent-2026-01-4", "2026-01-19", "Food", "Eq", "De", "20.00", "Rent: week 4/5 of 2026-01"),
            ("S-Rent-2026-01-5", "2026-01-26", "Food", "Eq", "De", "20.00", "Rent: week 5/5 of 2026-01"),
            ("S-Expenses-2026-01-2", "2026-01-05", "Food", "Eq", "De", "6.00", "Expenses: week 2/5 of 2026-01"),
            ("S-Expenses-2026-01-3", "2026-01-12", "Food", "Eq", "De", "6.00", "Expenses: week 3/5 of 2026-01"),
            ("S-Expenses-2026-01-4", "2026-01-19", "Food", "Eq", "De", "6.00", "Expenses: week 4/5 of 2026-01"),
            ("S-Expenses-2026-01-5", "2026-01-26", "Food", "Eq", "De", "6.00", "Expenses: week 5/5 of 2026-01"),
            ("S-Gas-2026-01-2", "2026-01-05", "Food", "Eq", "De", "10.00", "Gas: week 2/5 of 2026-01"),
            ("S-Gas-2026-01-3", "2026-01-12", "Food", "Eq", "De", "10.00", "Gas: week 3/5 of 2026-01"),
            ("S-Gas-2026-01-4", "2026-01-19", "Food", "Eq", "De", "10.00", "Gas: week 4/5 of 2026-01"),
            ("S-Gas-2026-01-5", "2026-01-26", "Food", "Eq", "De", "10.00", "Gas: week 5/5 of 2026-01"),
            # Up to the Monday of today's week, the third of 2026-02; empty values are none
            ("S-Expenses-2026-02-1", "2026-02-02", "Food", "Eq", "De", "7.50", "Expenses: week 1/4 of 2026-02"),
            ("S-Expenses-2026-02-2", "2026-02-09", "Food", "Eq", "De", "7.50", "Expenses: week 2/4 of 2026-02"),
        ]
        creditors = LedgerStore(settings).load().debt_accounts()[0].creditors
        assert creditors["Rent"].amount == 80 and creditors["Expenses"].amount == 24 + 15
        assert creditors["Gas"].amount == 5 + 40

    def test_the_month_of_a_week_is_that_of_its_thursday(self, settings, monkeypatch):
        monkeypatch.setattr("pledger.web.store.common.today", lambda: dt.date(2026, 6, 30))
        # Monday 2026-04-27 has its Thursday in April, and Monday 2026-05-25 in May
        settings.services_path.write_text(SERVICES_HEADER + "2026-05;10;;\n")
        assert [(r[0], r[1], r[5]) for r in self.owed(settings)] == [
            ("S-Rent-2026-05-1", "2026-05-04", "2.50"), ("S-Rent-2026-05-2", "2026-05-11", "2.50"),
            ("S-Rent-2026-05-3", "2026-05-18", "2.50"), ("S-Rent-2026-05-4", "2026-05-25", "2.50"),
        ]

    def test_weeks_split_the_cents(self, settings, monkeypatch):
        monkeypatch.setattr("pledger.web.store.common.today", lambda: dt.date(2026, 3, 31))
        settings.services_path.write_text(SERVICES_HEADER + "2026-02;10.01;;\n")
        assert [r[5] for r in self.owed(settings)] == ["2.51", "2.50", "2.50", "2.50"]

    def test_not_in_the_previous_month(self, settings):
        path = settings.accounts_path
        path.write_text("previous_month: 2025-12\n" + path.read_text().replace("    initial_date: 2025-12-31\n", ""))
        assert [r[0] for r in self.owed(settings)][:2] == ["S-Rent-2026-01-1", "S-Rent-2026-01-2"]

    @pytest.mark.parametrize(
        ("old", "new", "message"),
        [
            ("month;Rent;", "month;Water;", "columns ['Water'] aren't services of De with a base movement"),
            ("2026-02;;30;\n", "2026-02;;30;\n2026-01;1;;\n", "more than one row of 2026-01"),
            ("2026-02;;30;", "2026-2;;30;", "Invalid month of the services CSV"),
            ("2026-02;;30;", "2026-02;;-30;", "value of Expenses in 2026-02 of the services CSV cannot be negative"),
        ],
    )
    def test_invalid_csv(self, settings, old, new, message):
        text = settings.services_path.read_text()
        settings.services_path.write_text(text.replace(old, new, 1))
        with pytest.raises(LedgerDataError, match=re.escape(message)):
            LedgerStore(settings).load()

    @pytest.mark.parametrize(
        ("old", "new", "message"),
        [
            ("      amount: 5\n      base_movement: Food\n", "      base_movement: Salary\n", "must be a loss, not internal, that may credit De: not 'Salary' (Gas)"),
            ("      amount: 5\n      base_movement: Food\n", "      base_movement: Card fee\n", "not 'Card fee'"),
            ("      amount: 5\n      base_movement: Food\n", "      base_movement: Nope\n", "not 'Nope'"),
            ("      amount: 5\n      base_movement: Food\n", "      base_movement: 3\n", "'Gas' of the account 'De' base_movement must be a name: 3"),
            ("      amount: 5\n", "      amount: 5\n      day: 31\n", "'Gas' of the account 'De' has unknown keys ['day']"),
            ("      amount: 5\n", "      amount: 5\n      columns: {Gas: Food}\n", "'Gas' of the account 'De' has unknown keys ['columns']"),
            ("    Water: 0\n", "  people:\n    Dad:\n      amount: 0\n      base_movement: Food\n", "'Dad' of the account 'De' has unknown keys ['base_movement']"),
        ],
    )
    def test_invalid_services(self, settings, old, new, message):
        text = settings.creditors_path.read_text()
        if "people" in new:
            text = text.replace("  people:\n    Dad: 0\n", "")
        settings.creditors_path.write_text(text.replace(old, new, 1))
        with pytest.raises(LedgerDataError, match=re.escape(message)):
            LedgerStore(settings).load()

    def test_set_values(self, settings):
        store = LedgerStore(settings)
        store.set_service_value("2026-02", "Rent", "80")  # the others of its row kept
        store.set_service_value("2026-01", " Expenses ", "")  # cleared
        store.set_service_value("2025-11", "Gas", "1,000")  # a new month, before the others
        store.save_service_month("2026-03", {"Gas": "7", "Rent": ""})  # a new month, after them
        assert settings.services_path.read_text() == (
            SERVICES_HEADER + "2025-11;;;1,000.00\n2025-12;100;;\n2026-01;100.00;;50.00\n2026-02;80.00;30.00;\n"
            "2026-03;;;7.00\n"
        )
        store.save_service_month("2026-01", {"Gas": "5"})  # the whole month, replaced
        assert "\n2026-01;;;5.00\n" in settings.services_path.read_text()
        assert [r[5] for r in self.owed(settings) if r[0].startswith("S-Rent-2026-02")] == ["20.00", "20.00"]
        for args, error in [
            (("2026-02", "Water", "1"), "'Water' is not a service of De with a base movement"),
            (("2026-02", "Rent", "x"), "Invalid value of Rent"),
            (("2026-02", "Rent", "-1"), "cannot be negative"),
            (("2026-2", "Rent", "1"), "Invalid month"),
        ]:
            with pytest.raises(ValueError, match=re.escape(error)):
                store.set_service_value(*args)

    def test_a_new_column_and_csv(self, settings):
        settings.services_path.unlink()
        settings.creditors_path.write_text(settings.creditors_path.read_text().replace("    Water: 0\n", "    Water:\n      base_movement: Food\n"))
        store = LedgerStore(settings)
        store.set_service_value("2026-01", "Rent", "1")
        store.set_service_value("2026-01", "Water", "2")  # its column is added
        assert settings.services_path.read_text() == "month;Rent;Water\n2026-01;1.00;2.00\n"

    def test_services_tab(self, settings, client):
        r = client.get("/services")
        assert r.status_code == 200 and 'href="/services" aria-current="page"' in r.text
        assert card_labels(r.text) == ["This month's value", "Owed this month", "We owe them"]
        cards = r.text.split('<div class="cards">')[1].split("</div>\n  </div>")[0]
        assert "$30.00" in cards and "$15.00" in cards and "$164.00" in cards  # 80 + 39 + 45
        table = r.text.split('data-sortable="services"')[1].split("</table>")[0]
        assert "<td>Food</td>" in table and "None: add its <code>base_movement</code>" in table  # Water
        values = r.text.split('data-sortable="service-values"')[1].split("</table>")[0]
        assert "<th class=\"num\">Expenses</th>" in values and "2026-02" in values
        form = r.text.split('id="service-form"')[1].split("</form>")[0]
        # The month after the last one, a field per service, and filled in from the last month
        assert "<h2>New month</h2>" in form and 'value="2026-03"' in form
        assert 'name="service:Expenses"' in form and "Water" not in form
        assert "Fill in from 2026-02" in form and '"service:Expenses": "30.00"' in html.unescape(form)
        assert 'hx-get="/services/form?month=2026-02" hx-target="#service-form"' in values  # its ✎
        moves = client.get("/services", params={"service": "Rent"}, headers=HTMX).text
        assert "<html" not in moves and "<h2>Movements <small>4</small></h2>" in moves
        assert 'id="movement-S-Rent-2026-01-5"' in moves and 'data-sort="80.0">$80.00</td>' in moves  # owed after it
        index = client.get("/", params={"month": ""}).text
        assert 'href="/services#movement-S-Rent-2026-01-2" title="Generated from the monthly value of the service' in index

    def test_form_and_api(self, settings, client):
        r = client.get("/services/form", params={"month": "2026-01"})
        assert "<h2>Edit 2026-01</h2>" in r.text and "Save 2026-01" in r.text and "Fill in from" not in r.text
        # Cancel gets a new month form back
        assert '<button type="button" class="secondary" hx-get="/services/form" hx-target="#service-form" hx-swap="outerHTML">Cancel</button>' in r.text
        assert "<h2>New month</h2>" in client.get("/services/form").text
        assert "Cancel</button>" not in client.get("/services/form").text
        assert 'name="service:Rent" inputmode="decimal" value="100.00"' in r.text
        assert 'name="service:Expenses" inputmode="decimal" value=""' not in r.text  # 30.00
        r = client.post("/services/months", data={"month": "2026-03", "service:Rent": "120", "service:Gas": ""})
        assert "Saved 2026-03" in r.text and r.headers["HX-Trigger"] == "services-changed"
        assert "<h2>Edit 2026-03</h2>" in r.text  # now there
        assert settings.services_path.read_text().endswith("2026-03;120.00;;\n")
        r = client.post("/services/months", data={"month": "2026-03", "service:Rent": "x"})
        assert 'class="msg error"' in r.text and "Invalid value of Rent" in r.text and 'value="x"' in r.text  # kept
        assert client.put("/api/services", json={"month": "2026-03", "service": "Gas", "amount": 4}).status_code == 200
        assert client.put("/api/services", json={"month": "2026-03", "service": "Nope"}).status_code == 422
        data = client.get("/api/services").json()
        assert data["columns"] == ["Rent", "Expenses", "Gas"]
        assert [(v["month"], v["cells"]) for v in data["values"]][:2] == [
            ("2026-03", [120.0, None, 4.0]), ("2026-02", [None, 30.0, None]),
        ]

    def test_no_services(self, settings, client):
        settings.creditors_path.write_text("De:\n  people:\n    Dad: 0\n")
        settings.services_path.write_text("month\n")
        assert "No services yet" in client.get("/services").text
