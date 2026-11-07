"""Tests for the Accounts tab: editing the accounts."""


import pytest
import yaml
from conftest import INTERNAL_ACCOUNTS_YAML
from helpers import HTMX

from pledger.web.store import (
    LedgerStore,
)


class TestEditAccounts:
    def test_pencil_on_assets_and_liabilities_only(self, client):
        r = client.get("/accounts")
        assert 'aria-label="Edit account Ca"' in r.text and 'aria-label="Edit account Cc"' in r.text
        assert 'aria-label="Edit account Eq"' not in r.text
        assert client.get("/accounts/edit", params={"name": "Eq"}).status_code == 404
        assert client.get("/accounts/edit", params={"name": "Nope"}).status_code == 404

    def test_form_filled_with_the_account(self, client):
        r = client.get("/accounts/edit", params={"name": "Bk"}, headers=HTMX)
        form = r.text.split('id="account-form"')[1].split("</form>")[0]
        assert 'hx-post="/accounts/edit"' in form and "Edit account" in form and ">Cancel</button>" in form
        assert 'name="name" value="Bk" readonly' in form and 'name="acc_type"' not in form
        assert 'name="initial_amount" inputmode="decimal" value="0.1005"' in form
        assert 'name="initial_date" value="2025-12-31"' in form and 'name="currency"' not in form  # the currency stays
        assert '<input value="USD" readonly' in form

    def test_edit_an_internal_account(self, settings):
        path = settings.accounts_path
        block = (
            "internal:\n  asset:\n    Fi:\n      description: Intermediary\n      initial_amount: 0\n"
            "      checks:\n      - net_zero\n    Lo:\n      description: Loans\n  liability:\n    De:\n      description: Debts\n"
            "  equity:\n    Eq:\n      description: Equity\n"
        )
        path.write_text(path.read_text().replace(INTERNAL_ACCOUNTS_YAML, block))
        LedgerStore(settings).edit_account("Fi", "Between movements", "0", False, "", ["net_zero"])
        assert "internal:\n  asset:\n    Fi:\n      description: Between movements\n" in path.read_text()
        fi = LedgerStore(settings).load().accounts["Fi"]
        assert fi.internal and fi.description == "Between movements"

    def test_edit_keeps_the_rest_of_the_file(self, settings, client):
        path = settings.accounts_path
        path.write_text("# mine\n" + path.read_text().replace("    initial_date: 2025-12-31\n", "    initial_date: 2025-12-31\n    color: blue\n"))
        r = client.post("/accounts/edit", data={
            "name": "Bk", "description": "My bank", "initial_amount": "200", "currency": "USD", "is_liquid": "true",
            "initial_date": "", "check": "net_positive",
        }, headers=HTMX)
        assert "Updated account Bk" in r.text and "New account" in r.text
        data = yaml.safe_load(path.read_text())
        assert data["asset"]["Bk"] == {
            "description": "My bank", "currency": "USD", "is_liquid": True, "initial_amount": 200.0,
            "color": "blue", "checks": ["net_positive"],
        }
        assert path.read_text().startswith("# mine\n" + INTERNAL_ACCOUNTS_YAML + "asset:\n  Ca:\n") and "liability:\n  Cc:\n" in path.read_text()
        assert LedgerStore(settings).load().accounts["Bk"].initial_amount == 200

    def test_invalid_edits_roll_back(self, settings, client):
        before = settings.accounts_path.read_text()
        r = client.post("/accounts/edit", data={"name": "Ca", "description": "Cash", "initial_amount": "x"}, headers=HTMX)
        assert 'class="msg error"' in r.text and "Edit account" in r.text
        # Food takes 50.25 out of Ca: it can't be never negative with less than that
        with pytest.raises(ValueError, match="Could not edit 'Ca'"):
            LedgerStore(settings).edit_account("Ca", "Cash", "-0", checks=["net_zero"])
        assert settings.accounts_path.read_text() == before
        with pytest.raises(ValueError, match="Only asset and liability accounts"):
            LedgerStore(settings).edit_account("Eq", "Equity")


class TestAccountFormSwaps:
    def test_edit_and_cancel_swap_only_the_form(self, client):
        r = client.get("/accounts/edit", params={"name": "Ca"}, headers=HTMX)
        assert r.text.lstrip().startswith('<form id="account-form"') and "Balances" not in r.text
        r = client.get("/form/account", headers=HTMX)
        assert "New account" in r.text and "Balances" not in r.text
        page = client.get("/accounts").text
        assert 'hx-trigger="accounts-changed from:body"' in page and '<div id="account-form-slot" hx-preserve="true">' in page

    def test_the_currency_stays(self, settings, client):
        r = client.post("/accounts/edit", data={"name": "Bk", "description": "Bank", "initial_amount": "100.5",
                                                "currency": "ARS", "is_liquid": "true"}, headers=HTMX)
        assert "Updated account Bk" in r.text
        assert LedgerStore(settings).load().accounts["Bk"].currency.name == "USD"

    def test_the_interest_frequency_is_optional(self, settings, client):
        form = client.get("/accounts/edit", params={"name": "Ca"}).text.split('name="interest_frequency"')[1]
        assert '<option value="">None</option>' in form.split("</select>")[0]
        assert "selected" not in form.split("</select>")[0]  # none by default
        body = {"name": "Ca", "description": "Cash", "initial_amount": "0", "is_liquid": "true", "interest_frequency": "monthly"}
        assert "Updated account Ca" in client.post("/accounts/edit", data=body).text
        ca = yaml.safe_load(settings.accounts_path.read_text())["asset"]["Ca"]
        assert ca["interest_frequency"] == "monthly"
        assert LedgerStore(settings).load().accounts["Ca"].interest_frequency.value == "monthly"
        assert 'value="monthly" selected' in client.get("/accounts/edit", params={"name": "Ca"}).text
        client.post("/accounts/edit", data={**body, "interest_frequency": ""})
        assert "interest_frequency" not in yaml.safe_load(settings.accounts_path.read_text())["asset"]["Ca"]
        assert LedgerStore(settings).load().accounts["Ca"].interest_frequency is None
        r = client.post("/accounts/edit", data={**body, "interest_frequency": "daily"})
        assert "Invalid interest frequency: &#39;daily&#39;" in r.text


class TestInternalAccounts:
    def test_are_marked_as_such(self, client):
        r = client.get("/accounts")
        badge = '<span class="badge internal"'
        rows = {
            row.split("<strong>")[1].split("</strong>")[0]: badge in row.split("</td>")[0]
            for row in r.text.split('<tr id="account-')[1:]
        }
        assert rows == {"Ca": False, "Bk": False, "Fi": True, "Lo": True, "Cc": False, "De": True, "Eq": True}
        # In the bars too
        bars = r.text.split('class="composition-pair"')[1].split("</section>")[0]
        assert 'Fi <span class="badge internal"' in bars and 'Ca <span class="badge internal"' not in bars
