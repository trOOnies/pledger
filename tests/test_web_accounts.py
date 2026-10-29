"""Tests for the Accounts tab: editing the accounts and correcting their balances."""

import datetime as dt
from dataclasses import replace

import pytest
import yaml
from conftest import INTERNAL_ACCOUNTS_YAML
from fastapi.testclient import TestClient
from helpers import CORRECTION_YAML, HTMX, INVEST_TRANSFERS_YAML, rows, with_internal

from pledger.web.app import create_app
from pledger.web.store import (
    LedgerStore,
    row_fingerprint,
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
            "      checks:\n      - net_zero\n  liability:\n    De:\n      description: Debts\n"
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


class TestBalanceCorrections:
    @pytest.fixture
    def settings(self, settings, tmp_path):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace("liability:\n", "  Br:\n    description: Broker\nliability:\n"), encoding="utf-8"
        )
        path = settings.base_movements_path
        with_internal(path, CORRECTION_YAML, INVEST_TRANSFERS_YAML)
        investments = tmp_path / "investment.yaml"
        investments.write_text("Br:\n  T: stock\n", encoding="utf-8")
        return replace(
            settings, investments_path=investments, investment_movements_path=tmp_path / "investment.csv",
            interest_path=tmp_path / "interest.csv",
        )

    @pytest.fixture
    def client(self, settings):
        return TestClient(create_app(settings))

    @staticmethod
    def last(settings) -> list:
        return rows(settings).iloc[-1][["move_date", "base_movement", "acc_debit", "amount", "acc_credit"]].tolist()

    def test_corrects_to_the_checked_balance(self, settings):
        store = LedgerStore(settings)
        # Ca: 159.75 at the end of 2026-01-04
        assert store.correct_balance("Ca", "160", "2026-01-04") == ("Correction (G)", pytest.approx(0.25))
        assert self.last(settings) == ["2026-01-04", "Correction (G)", "Ca", "0.25", "Eq"]
        assert store.correct_balance("Ca", "160", "2026-01-04") is None  # already right
        today = dt.date.today().isoformat()
        assert store.correct_balance("Ca", "150") == ("Correction (L)", pytest.approx(10))
        assert self.last(settings) == [today, "Correction (L)", "Eq", "10.00", "Ca"]
        assert rows(settings).iloc[-1]["comment"] == "Checked balance: 150.00 ARS"
        # A liability: owing more is a loss
        assert store.correct_balance("Cc", "30") == ("Correction (L)", pytest.approx(30))
        assert self.last(settings)[2:] == ["Eq", "30.00", "Cc"]
        assert store.load().ledger().balances()["Cc"] == pytest.approx(30)

    def test_in_another_currency(self, settings):
        # Bk: 0.1005 + 1.0005 - 0.2 = 0.901 USD, at 1,000 ARS
        assert LedgerStore(settings).correct_balance("Bk", "1", "2026-01-05") == ("Correction (G)", pytest.approx(99))
        assert LedgerStore(settings).load().own_balance("Bk", "2026-01-05") == pytest.approx(1)

    def test_invalid(self, settings):
        store = LedgerStore(settings)
        for name, balance, date, error in (
            ("Br", "1", "", "comes from its investments"),
            ("Eq", "1", "", "Only the balances of asset and liability accounts"),
            ("Ca", "", "", "The checked balance is required"),
            ("Ca", "x", "", "Invalid checked balance"),
            ("Ca", "1", "2026-13-01", "Invalid check date"),
        ):
            with pytest.raises(ValueError, match=error):
                store.correct_balance(name, balance, date)

    def test_form(self, settings, client):
        page = client.get("/accounts").text
        assert 'aria-label="Correct the balance of Ca"' in page and 'aria-label="Correct the balance of Br"' not in page
        assert client.get("/accounts/correct", params={"name": "Br"}).status_code == 404
        r = client.get("/accounts/correct", params={"name": "Ca"}, headers=HTMX)
        assert "Correct balance" in r.text and 'hx-post="/accounts/correct"' in r.text and "ARS 159.75" in r.text
        r = client.post("/accounts/correct", data={"name": "Ca", "balance": "x"}, headers=HTMX)
        assert 'class="msg error"' in r.text and 'name="balance"' in r.text and 'value="x"' in r.text
        r = client.post("/accounts/correct", data={"name": "Ca", "balance": "200", "check_date": "2026-01-04"}, headers=HTMX)
        assert "Corrected Ca: Correction (G) of $40.25" in r.text and r.headers["HX-Trigger"] == "accounts-changed"
        r = client.post("/accounts/correct", data={"name": "Ca", "balance": "200", "check_date": "2026-01-04"}, headers=HTMX)
        assert "already right" in r.text and "HX-Trigger" not in r.headers

    def test_are_in_the_ledger_csv_and_deleted_from_the_ledger_tab(self, settings, client):
        # Like those added by hand before they were internal
        with settings.ledger_path.open("a") as f:
            f.write("2026-01-05;Correction (L);Eq;5;Ca;\n")
        LedgerStore(settings).freeze_ids()
        snapshot = LedgerStore(settings).load()
        assert "Correction (L)" not in snapshot.invest_base_movements()
        assert "Correction (L)" not in client.get("/form/bases", params={"move_type": "loss"}).text  # not picked by hand
        with pytest.raises(ValueError, match="added from the Accounts tab"):
            LedgerStore(settings).add("2026-01-05", "Correction (L)", "Eq", "Ca", "1")
        row = client.get("/").text.split('id="movement-L-5"')[1].split("</tr>")[0]
        assert 'href="/accounts#account-Ca"' in row and 'hx-post="/movements/4/delete"' in row and "&#9998;" not in row
        fp = row_fingerprint(rows(settings).iloc[4])
        assert client.post("/movements/4/delete", data={"fingerprint": fp}).status_code == 200
        assert len(rows(settings)) == 4

    def test_needs_the_correction_base_movements(self, settings):
        path = settings.base_movements_path
        path.write_text(path.read_text().replace("          Correction (G): c\n", ""))
        with pytest.raises(ValueError, match="Correcting balances needs 'Correction \\(G\\)'"):
            LedgerStore(settings).correct_balance("Ca", "200")
