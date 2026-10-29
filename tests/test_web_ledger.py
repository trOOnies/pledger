"""Tests for the Ledger tab: its balances, filters and search."""

import pytest

from pledger.web.store import (
    LedgerStore,
)
from pledger.web.views import (
    build_view,
)


class TestLedgerBalances:
    def test_balances_after_each_movement(self, settings):
        moves = build_view(LedgerStore(settings).load())["movements"]
        # Newest first: the gift, the food, the withdrawal and the salary, after Bk's initial 100.5
        assert [(m["base"], m["debit_balance"], m["credit_balance"]) for m in moves] == [
            ("Gift", pytest.approx(159.75), pytest.approx(1060.75)),
            ("Food", pytest.approx(1050.75), pytest.approx(149.75)),
            ("Withdraw", pytest.approx(200), pytest.approx(901)),
            ("Salary", pytest.approx(1101), pytest.approx(1101)),
        ]

    def test_are_of_the_whole_ledger_whatever_the_filters(self, settings):
        moves = build_view(LedgerStore(settings).load(), "2026-01", "Bk")["movements"]
        assert [(m["base"], m["credit_balance"]) for m in moves] == [("Withdraw", pytest.approx(901)), ("Salary", pytest.approx(1101))]

    def test_columns(self, client):
        table = client.get("/").text.split('data-sortable="movements"')[1].split("</table>")[0]
        assert "Debit balance</th>" in table and "Credit balance</th>" in table
        assert '<td class="num money balance " data-sort="159.75">$159.75</td>' in table


class TestLedgerSearch:
    def bases(self, settings, q, **filters) -> list[str]:
        return [m["base"] for m in build_view(LedgerStore(settings).load(), q=q, **filters)["movements"]]

    def test_matches_every_field(self, settings):
        LedgerStore(settings).add("2026-01-05", "Food", "Eq", "Ca", "12", "Rent of the garage")
        assert self.bases(settings, "garage") == ["Food"]  # the comment, case-folded
        assert self.bases(settings, "GIFT") == ["Gift"]  # the base movement
        assert self.bases(settings, "2026-01-02") == ["Withdraw"]  # the date
        assert self.bases(settings, "L-1") == ["Salary"]  # the id
        assert self.bases(settings, "1,000.50") == self.bases(settings, "1000.5") == ["Salary"]
        assert self.bases(settings, "bk") == ["Withdraw", "Salary"]  # the accounts

    def test_every_term_must_match(self, settings):
        LedgerStore(settings).add("2026-01-05", "Food", "Eq", "Ca", "12", "Rent of the garage")
        assert self.bases(settings, "food garage") == ["Food"]
        assert self.bases(settings, "food") == ["Food", "Food"]
        assert self.bases(settings, '"garage rent"') == []  # a quoted phrase is one term
        assert self.bases(settings, '"rent of"') == ["Food"]
        assert self.bases(settings, '"rent of') == ["Food"]  # an unclosed quote is ignored
        LedgerStore(settings).add("2026-01-06", "Gift", "Ca", "Eq", "5", "From O'Brien")
        assert self.bases(settings, "o'brien") == ["Gift"]
        assert self.bases(settings, "  ") == ["Gift", "Food", "Gift", "Food", "Withdraw", "Salary"]

    def test_with_the_other_filters_and_totals(self, settings):
        assert self.bases(settings, "ca", account="Bk") == ["Withdraw"]
        view = build_view(LedgerStore(settings).load(), q="eq")
        assert view["gains"] == pytest.approx(1010.5) and view["losses"] == pytest.approx(-50.25)
        assert view["movements"][0]["credit_balance"] == pytest.approx(1060.75)  # of the whole ledger

    def test_page_and_api(self, client):
        r = client.get("/", params={"q": "withdraw"})
        assert 'name="q" value="withdraw"' in r.text
        table = r.text.split('data-sortable="movements"')[1].split("</table>")[0]
        assert "Withdraw" in table and "Salary" not in table
        partial = client.get("/", params={"q": "nothing like this"}, headers={"HX-Request": "true"})
        assert "No movements." in partial.text
        assert [m["base"] for m in client.get("/api/movements", params={"q": "food"}).json()] == ["Food"]


class TestLedgerBaseFilter:
    def test_filter_by_base_movement(self, settings, client):
        moves = build_view(LedgerStore(settings).load(), base="Food")["movements"]
        assert [m["base"] for m in moves] == ["Food"]
        r = client.get("/", params={"base": "Food"})
        assert '<option value="Food" selected>Food</option>' in r.text
        assert "<h2>Movements <small>1</small></h2>" in r.text
