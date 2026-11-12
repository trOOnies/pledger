"""Tests for the Ledger tab: its balances, filters and search."""

import datetime as dt
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import HTMX, MOVEMENT, WORK_CSV, with_salaries

from pledger.web.app import create_app
from pledger.web.store import (
    LedgerStore,
    row_fingerprint,
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


class TestCopyMovement:
    @staticmethod
    def row(settings, index):
        return LedgerStore(settings).load().rows.iloc[index]

    def test_editable_movements_have_the_button(self, client):
        table = client.get("/").text.split('data-sortable="movements"')[1].split("</table>")[0]
        assert table.count('title="Copy"') == table.count('title="Edit"') == 4
        assert 'hx-get="/movements/2/copy"' in table

    def test_fills_in_a_new_movement_form(self, client, settings):
        LedgerStore(settings).freeze_ids()
        row = self.row(settings, 2)
        form = client.get("/movements/2/copy", params={"fingerprint": row_fingerprint(row)}, headers=HTMX).text
        assert "<h2>New movement</h2>" in form and "Edit movement" not in form
        assert 'hx-post="/movements"' in form and 'name="fingerprint"' not in form
        assert f'name="move_date" value="{row.move_date}"' in form
        assert f'name="amount" inputmode="decimal" min="0" value="{row.amount}"' in form
        assert "Filled in from L-3" in form

    def test_a_stale_row_isnt_copied(self, client):
        assert client.get("/movements/2/copy", params={"fingerprint": "x"}).status_code == 409

    def test_adding_it_is_a_new_movement(self, client, settings):
        before = LedgerStore(settings).load().rows
        row = before.iloc[2]
        client.get("/movements/2/copy", params={"fingerprint": row_fingerprint(row)})
        assert len(LedgerStore(settings).load().rows) == len(before)  # copying saves nothing
        for date in (row.move_date, "2026-02-01"):  # the date can stay
            r = client.post("/movements", data={
                "move_date": date, "base_movement": row.base_movement, "acc_debit": row.acc_debit,
                "acc_credit": row.acc_credit, "amount": row.amount, "comment": row.comment,
            }, headers=HTMX)
            assert "Added " in r.text and "Filled in from" not in r.text
        rows = LedgerStore(settings).load().rows
        assert len(rows) == len(before) + 2
        assert rows.iloc[2][list(before.columns)].equals(row)  # the original is left as it was
        assert list(rows.move_date[-2:]) == [row.move_date, "2026-02-01"]


class TestEditableFilter:
    @pytest.fixture
    def settings(self, settings):
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write("2026-02-01;Food;Eq;1;Ca;\n")
        return settings

    def test_only_the_editable_ones(self, settings):
        snapshot = LedgerStore(settings).load()
        moves = build_view(snapshot, initial=True)["movements"]
        kept = build_view(snapshot, initial=True, editable=True)["movements"]
        assert len(kept) == sum(m["editable"] for m in moves) < len(moves)
        assert all(m["editable"] for m in kept)

    def test_not_the_generated_ones_of_a_regular_base_movement(self, settings, tmp_path, monkeypatch):
        monkeypatch.setattr("pledger.web.store.common.today", lambda: dt.date(2026, 2, 15))
        creditors = tmp_path / "creditors.yaml"
        creditors.write_text("services:\n  Rent:\n    amount: 0\n    base_movement: Food\n", encoding="utf-8")
        services = tmp_path / "services.csv"
        services.write_text("month;Rent\n2026-01;100\n", encoding="utf-8")
        snapshot = LedgerStore(replace(settings, creditors_path=creditors, services_path=services)).load()
        moves = build_view(snapshot)["movements"]
        generated = [m for m in moves if m["base"] == "Food" and m["source"]]
        assert generated and not any(m["editable"] for m in generated)
        assert {m["source"] for m in build_view(snapshot, editable=True)["movements"]} == {None}

    def test_keeps_the_balances_and_works_with_the_search(self, settings):
        snapshot = LedgerStore(settings).load()
        everything = {m["index"]: m for m in build_view(snapshot)["movements"]}
        for m in build_view(snapshot, editable=True, q="food")["movements"]:
            assert m["base"] == "Food" and m["debit_balance"] == everything[m["index"]]["debit_balance"]

    def test_the_tab_has_it(self, client):
        assert 'name="editable" value="true" >' in client.get("/").text
        assert 'name="editable" value="true" checked' in client.get("/", params={"editable": "true"}).text


class TestAutoBaseMovements:
    """The base movements whose movements are created elsewhere can't get new ones from the Ledger tab."""

    @pytest.fixture
    def settings(self, settings, tmp_path, monkeypatch):
        monkeypatch.setattr("pledger.web.store.common.today", lambda: dt.date(2026, 2, 15))
        creditors = tmp_path / "creditors.yaml"
        creditors.write_text("services:\n  Rent:\n    amount: 0\n    base_movement: Food\n", encoding="utf-8")
        services = tmp_path / "services.csv"
        services.write_text("month;Rent\n2026-01;100\n", encoding="utf-8")
        work = tmp_path / "work.csv"
        work.write_text(WORK_CSV, encoding="utf-8")
        return replace(with_salaries(settings), creditors_path=creditors, services_path=services, work_path=work)

    def test_are_classified_by_the_tab_that_creates_them(self, settings):
        snapshot = LedgerStore(settings).load()
        auto = snapshot.auto_base_movements()
        assert {n: auto[n] for n in ("Food", "Tax", "Work - Salary Main", "Initial (G)")} == {
            "Food": "Services", "Tax": "Work", "Work - Salary Main": "Work", "Initial (G)": "Accounts",
        }
        assert "Card fee" not in auto and "Gift" not in auto
        ledger = snapshot.ledger_base_movements()
        assert "Food" not in ledger and "Tax" not in ledger and "Card fee" in ledger

    @pytest.mark.parametrize(("base", "debit", "credit", "tab"), [("Food", "Eq", "Ca", "Services"), ("Tax", "Eq", "Ca", "Work")])
    def test_cannot_be_added_from_the_ledger_tab(self, settings, base, debit, credit, tab):
        with pytest.raises(ValueError, match=f"{base!r} are created from the {tab} tab"):
            LedgerStore(settings).add("2026-02-02", base, debit, credit, "1")

    def test_arent_in_the_form(self, settings):
        client = TestClient(create_app(settings))
        form = client.get("/form/bases", params={"move_type": "loss"}).text
        assert ">Card fee<" in form and ">Food<" not in form and ">Tax<" not in form
        r = client.post("/movements", data={**MOVEMENT, "base_movement": "Food", "acc_debit": "Eq", "acc_credit": "Ca"})
        assert "are created from the Services tab" in r.text


class TestGroupedSelects:
    def test_filters_by_account_type_and_result_type(self, client):
        filters = client.get("/").text.split('id="filters"')[1].split("</form>")[0]
        accounts = filters.split('name="account"')[1].split("</select>")[0]
        groups = [g.split('"')[0] for g in accounts.split('<optgroup label="')[1:]]
        assert groups == [
            "General assets", "Foreign currency assets (USD)", "General liabilities", "Debts &amp; loans",
            "Debts &amp; loans (USD)", "Equity",  # the debt accounts in other currencies by currency too
        ]
        bases = filters.split('name="base"')[1].split("</select>")[0]
        groups = [g.split('"')[0] for g in bases.split('<optgroup label="')[1:]]
        assert groups == ["Operative", "Gifts", "Initial", "Transfers"]
        operative = bases.split('<optgroup label="Operative">')[1].split("</optgroup>")[0]
        assert operative.index(">Card fee<") < operative.index(">Food<") < operative.index(">Salary<")

    def test_base_movement_field_by_result_type(self, client):
        form = client.get("/").text.split('id="base-fields"')[1].split("</select>")[0]
        # The losses by result type, the first one picked by default
        assert '<optgroup label="Operative">' in form and "<option selected>Card fee</option>" in form
        gains = client.get("/form/bases", params={"move_type": "gain"}).text.split("</select>")[0]
        assert '<optgroup label="Operative">' in gains and '<optgroup label="Gifts">' in gains
        # The transfers have no result type: no groups
        transfers = client.get("/form/bases", params={"move_type": "transfer"}).text.split("</select>")[0]
        assert "<optgroup" not in transfers and ">Move</option>" in transfers
