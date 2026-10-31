"""Tests for the Audit tab: the balances checked by hand and their corrections."""

import datetime as dt
import html
import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import CORRECTION_YAML, HTMX, INVEST_TRANSFERS_YAML, rows, with_internal

from pledger.web.app import create_app
from pledger.web.store import LedgerStore, row_fingerprint


def balance(**values):
    """The form fields of the balances filled in."""
    return {f"balance:{name}": value for name, value in values.items()}


class TestAudit:
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

    @staticmethod
    def row(text, name) -> str:
        return text.split(f"<strong>{name}</strong>")[1].split("</tr>")[0]

    def test_corrects_to_the_checked_balance(self, settings):
        store = LedgerStore(settings)
        today = dt.date.today().isoformat()
        actual = store.load().balances_today()["Ca"]
        assert store.correct_balance("Ca", actual + 0.25) == ("Correction (G)", pytest.approx(0.25))
        assert self.last(settings) == [today, "Correction (G)", "Ca", "0.25", "Eq"]
        assert store.correct_balance("Ca", actual + 0.25) is None  # already right
        assert store.correct_balance("Ca", actual - 10) == ("Correction (L)", pytest.approx(10.25))
        assert self.last(settings) == [today, "Correction (L)", "Eq", "10.25", "Ca"]
        assert rows(settings).iloc[-1]["comment"] == f"Checked balance: {actual - 10:,.2f} ARS"
        # A liability: owing more is a loss
        before = store.load().balances_today()["Cc"]
        assert store.correct_balance("Cc", before + 30) == ("Correction (L)", pytest.approx(30))
        assert self.last(settings)[2:] == ["Eq", "30.00", "Cc"]
        assert store.load().balances_today()["Cc"] == pytest.approx(before + 30)

    def test_in_another_currency(self, settings):
        store = LedgerStore(settings)
        # Bk: 0.901 USD, at 1,000 ARS
        assert store.correct_balance("Bk", "1") == ("Correction (G)", pytest.approx(99))
        assert store.load().balances_today()["Bk"] == pytest.approx(1)

    def test_invalid(self, settings):
        store = LedgerStore(settings)
        for name, value, error in (
            ("Br", "1", "comes from its investments"),
            ("Eq", "1", "Only the balances of asset and liability accounts"),
            ("Ca", "", "The checked balance is required"),
            ("Ca", "x", "Invalid checked balance"),
        ):
            with pytest.raises(ValueError, match=error):
                store.correct_balance(name, value)

    def test_form_is_blind(self, settings, client):
        balances = LedgerStore(settings).load().balances_today()
        r = client.get("/audit")
        assert r.status_code == 200 and 'href="/audit" aria-current="page"' in r.text
        assert 'hx-post="/audit"' in r.text and 'name="balance:Ca"' in r.text and 'name="balance:Cc"' in r.text
        assert 'name="balance:Br"' in r.text and 'name="balance:Eq"' not in r.text  # the equity is what they add up to
        assert f"{balances['Ca']:,.2f}" not in r.text
        assert 'name="balance:Bk"' in r.text and 'data-money="4"' in r.text  # in its own currency
        assert 'data-money="2"' in r.text

    def test_results(self, settings, client):
        actual = LedgerStore(settings).load().balances_today()
        before = settings.ledger_path.read_text()
        r = client.post("/audit", data=balance(Ca="100", Bk="", Cc=f"{actual['Cc']:.2f}"), headers=HTMX)
        assert "<html" not in r.text and settings.ledger_path.read_text() == before  # nothing is written
        ca = self.row(r.text, "Ca")
        assert "ARS 100.00" in ca and f"ARS {actual['Ca']:,.2f}" in ca and f"ARS {100 - actual['Ca']:,.2f}" in ca
        assert 'hx-post="/audit/correct"' in ca and '"account": "Ca"' in html.unescape(ca) and "Add Correction (L) of" in ca
        assert "Matches" in self.row(r.text, "Cc") and "/audit/correct" not in self.row(r.text, "Cc")
        assert "<strong>Bk</strong>" not in r.text  # left empty: not audited
        cards = re.findall(r'class="label">(.+?)</span><span class="num">(\d+)<', r.text)
        assert cards == [("Audited", "2"), ("With a difference", "1"), ("Not audited", "4")]
        assert 'name="balance:Ca" value="100"' in r.text and 'name="balance:Bk"' not in r.text

    def test_zero_is_audited(self, settings, client):
        actual = LedgerStore(settings).load().balances_today()["Ca"]
        ca = self.row(client.post("/audit", data=balance(Ca="0.00")).text, "Ca")
        assert "ARS 0.00" in ca and f"ARS {-actual:,.2f}" in ca and "Add Correction (L)" in ca

    def test_in_its_currency_with_its_correction_at_its_value(self, settings, client):
        bk = self.row(client.post("/audit", data=balance(Bk="1")).text, "Bk")
        assert "USD 1.0000" in bk and "USD 0.9010" in bk and "USD 0.0990" in bk and "Add Correction (G) of $99.00" in bk

    def test_invalid_balances_keep_the_form_blind(self, client):
        r = client.post("/audit", data=balance(Ca="x", Bk="1"))
        assert "Invalid balance of Ca" in r.text and 'value="x"' in r.text and 'value="1"' in r.text
        assert "Results" not in r.text
        r = client.post("/audit", data=balance(Ca=" "))
        assert "Fill in at least one balance" in r.text

    def test_investment_accounts_are_audited_but_not_corrected(self, client):
        br = self.row(client.post("/audit", data=balance(Br="5")).text, "Br")
        assert "Fix it in" in br and "/audit/correct" not in br
        r = client.post("/audit/correct", data={"account": "Br", **balance(Br="5")})
        assert "comes from its investments" in r.text

    def test_correcting_is_one_account_and_keeps_the_results(self, settings, client):
        count = len(rows(settings))
        data = balance(Ca="100", Cc="1000")
        r = client.post("/audit/correct", data={"account": "Ca", **data})
        assert len(rows(settings)) == count + 1 and self.last(settings)[1:3] == ["Correction (L)", "Eq"]
        assert self.last(settings)[0] == dt.date.today().isoformat()
        assert "Corrected Ca: Correction (L) of" in r.text
        assert "Matches" in self.row(r.text, "Ca")
        assert "Add Correction" in self.row(r.text, "Cc")  # the other one is left as it was
        r = client.post("/audit/correct", data={"account": "Ca", **data})  # clicked twice
        assert "already right" in r.text and len(rows(settings)) == count + 1

    def test_needs_the_correction_base_movements(self, settings, client):
        path = settings.base_movements_path
        path.write_text(path.read_text().replace("          Correction (L): c\n", ""))
        r = TestClient(create_app(settings)).post("/audit/correct", data={"account": "Ca", **balance(Ca="1")})
        assert "Correcting balances needs &#39;Correction (L)&#39;" in r.text

    def test_the_accounts_tab_no_longer_corrects(self, client):
        page = client.get("/accounts").text
        assert "Correct the balance" not in page and "/accounts/correct" not in page
        assert client.get("/accounts/correct", params={"name": "Ca"}).status_code == 404
        assert client.post("/accounts/correct", data={"name": "Ca", "balance": "1"}).status_code == 404

    def test_are_in_the_ledger_csv_and_deleted_from_the_ledger_tab(self, settings, client):
        # Like those added by hand before they were internal
        with settings.ledger_path.open("a") as f:
            f.write("2026-01-05;Correction (L);Eq;5;Ca;\n")
        LedgerStore(settings).freeze_ids()
        snapshot = LedgerStore(settings).load()
        assert "Correction (L)" not in snapshot.invest_base_movements()
        assert "Correction (L)" not in client.get("/form/bases", params={"move_type": "loss"}).text  # not picked by hand
        with pytest.raises(ValueError, match="added from the Audit tab"):
            LedgerStore(settings).add("2026-01-05", "Correction (L)", "Eq", "Ca", "1")
        row = client.get("/").text.split('id="movement-L-5"')[1].split("</tr>")[0]
        assert 'href="/accounts#account-Ca"' in row and 'hx-post="/movements/4/delete"' in row and "&#9998;" not in row
        fp = row_fingerprint(rows(settings).iloc[4])
        assert client.post("/movements/4/delete", data={"fingerprint": fp}).status_code == 200
        assert len(rows(settings)) == 4
