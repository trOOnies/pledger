"""Tests for the Debt tab and the debt account."""

import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import (
    DEBT_YAML,
    DIFFERENCE_YAML,
    HTMX,
    WORK_CSV,
    card_labels,
    movement_ids,
    rows,
    with_internal,
    with_salaries,
)

from pledger.web.app import create_app
from pledger.web.store import (
    LedgerDataError,
    LedgerStore,
    row_fingerprint,
)
from pledger.web.views import (
    build_debts,
    debt_form_fields,
)

FORM = {"move_date": "2026-02-10", "acc_debit": "Ca", "acc_credit": "De", "who": "Dad", "remaining_as_difference": "true"}


class TestRemainingAsDifference:
    """Settling what remains with someone after a debt movement, in the Debt tab."""


    @pytest.fixture
    def settings(self, settings, tmp_path):
        path = tmp_path / "creditors.yaml"
        path.write_text("people:\n  Dad: 0\n  Mum: 0\n", encoding="utf-8")
        with_internal(settings.base_movements_path, DIFFERENCE_YAML, DEBT_YAML)
        return replace(settings, creditors_path=path)

    @pytest.fixture
    def client(self, settings):
        return TestClient(create_app(settings))

    @staticmethod
    def balance(settings, who="Dad"):
        return sum(a.creditors[who].amount for a in LedgerStore(settings).load().debt_accounts() if who in a.creditors)

    def test_we_owe_less_is_a_gain(self, settings):
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "100", "Dad")
        assert store.add_debt_movement(
            "2026-02-05", "Debt - Paid", "De", "Ca", "90", "Dad", "last payment", remaining_as_difference=True,
        ) == ("Difference (G)", 10.0)
        last = rows(settings).iloc[-1]
        assert (last.move_date, last.base_movement, last.acc_debit, last.acc_credit) == ("2026-02-05", "Difference (G)", "De", "Eq")
        assert last.comment == "Dad: last payment" and last.amount == "10.00"
        assert self.balance(settings) == pytest.approx(0)

    def test_they_owe_us_less_is_a_loss(self, settings):
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Loan - Given", "Lo", "Ca", "100", "Dad")
        assert store.add_debt_movement(
            "2026-02-05", "Loan - Collected", "Ca", "Lo", "90", "Dad", remaining_as_difference=True,
        ) == ("Difference (L)", 10.0)
        last = rows(settings).iloc[-1]
        assert (last.base_movement, last.acc_debit, last.acc_credit, last.comment) == ("Difference (L)", "Eq", "Lo", "Dad")
        assert self.balance(settings) == pytest.approx(0)

    def test_paying_too_much_is_a_loss(self, settings):
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "100", "Dad")
        assert store.add_debt_movement(
            "2026-02-05", "Debt - Paid", "De", "Ca", "120", "Dad", remaining_as_difference=True,
        ) == ("Difference (L)", 20.0)
        assert self.balance(settings) == pytest.approx(0)

    def test_nothing_remaining_adds_no_difference(self, settings):
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "100", "Dad")
        before = len(rows(settings))
        assert store.add_debt_movement(
            "2026-02-05", "Debt - Paid", "De", "Ca", "100", "Dad", remaining_as_difference=True,
        ) is None
        assert len(rows(settings)) == before + 1

    def test_off_by_default(self, settings):
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "100", "Dad")
        assert store.add_debt_movement("2026-02-05", "Debt - Paid", "De", "Ca", "90", "Dad") is None
        assert self.balance(settings) == pytest.approx(10)

    def test_only_of_that_person(self, settings):
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "100", "Mum")
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "100", "Dad")
        store.add_debt_movement("2026-02-05", "Debt - Paid", "De", "Ca", "90", "Dad", remaining_as_difference=True)
        assert self.balance(settings) == pytest.approx(0) and self.balance(settings, "Mum") == pytest.approx(100)

    def test_neither_row_added_if_invalid(self, settings):
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "100", "Dad")
        before = len(rows(settings))
        with pytest.raises(ValueError, match="Invalid date"):
            store.add_debt_movement("nope", "Debt - Paid", "De", "Ca", "90", "Dad", remaining_as_difference=True)
        assert len(rows(settings)) == before

    def test_needs_the_base_movement_only_when_used(self, settings):
        path = settings.base_movements_path
        path.write_text(path.read_text().replace("          Difference (G): d\n", ""))
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "100", "Dad")
        before = len(rows(settings))
        with pytest.raises(ValueError, match=re.escape("needs 'Difference (G)', an internal gain")):
            store.add_debt_movement("2026-02-05", "Debt - Paid", "De", "Ca", "90", "Dad", remaining_as_difference=True)
        assert len(rows(settings)) == before
        form = TestClient(create_app(settings)).get("/debt").text.split('id="debt-form"')[1].split("</form>")[0]
        assert 'name="remaining_as_difference"' not in form and "Difference (G)" in form

    def test_not_added_on_its_own(self, settings, client):
        store = LedgerStore(settings)
        with pytest.raises(ValueError, match="'Difference \\(G\\)' is not a debt base movement"):
            store.add_debt_movement("2026-02-05", "Difference (G)", "De", "Eq", "1", "Dad")
        with pytest.raises(ValueError, match="are added from the Debt tab"):
            store.add("2026-02-05", "Difference (G)", "De", "Eq", "1", "Dad")
        assert "Difference" not in client.get("/form/bases", params={"move_type": "gain"}).text

    def test_form(self, settings, client):
        LedgerStore(settings).add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "100", "Dad")
        form = client.get("/debt").text.split('id="debt-form"')[1].split("</form>")[0]
        assert 'name="remaining_as_difference"' in form and "Set remaining as Difference" in form
        assert 'hx-get="/form/debt-difference"' in form and "load," not in form
        r = client.post("/debt/movements", data={**FORM, "base_movement": "Debt - Paid", "acc_debit": "De", "acc_credit": "Ca", "amount": "90"})
        assert "Added Debt - Paid (Dad): $90.00 and Difference (G): $10.00" in r.text
        assert 'name="remaining_as_difference" value="true" >' in r.text  # not kept for the next one
        assert self.balance(settings) == pytest.approx(0)

    def test_form_keeps_it_on_an_error(self, client):
        r = client.post("/debt/movements", data={**FORM, "base_movement": "Debt - Paid", "acc_debit": "De", "acc_credit": "Ca", "amount": "-1"})
        assert 'class="msg error"' in r.text and 'value="true" checked' in r.text and "hx-trigger=\"load," in r.text

    def test_api(self, settings, client):
        LedgerStore(settings).add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "100", "Dad")
        r = client.post("/api/debt/movements", json={
            "move_date": "2026-02-05", "base_movement": "Debt - Paid", "acc_debit": "De", "acc_credit": "Ca",
            "amount": 90, "who": "Dad", "remaining_as_difference": True,
        })
        assert r.status_code == 201
        assert r.json() == {"ok": True, "difference": {"base_movement": "Difference (G)", "amount": 10.0}}

    def test_preview(self, settings, client):
        LedgerStore(settings).add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "100", "Dad")

        def preview(**params):
            return client.get("/form/debt-difference", params={"base_movement": "Debt - Paid", "who": "Dad", **params}).text

        assert preview(amount="90").strip() == ""  # toggled off
        on = {"remaining_as_difference": "true"}
        assert "Pick who and the amount" in preview(**on, amount="")
        assert "Pick who and the amount" in preview(**on, amount="abc")
        assert "Pick who and the amount" in preview(**on, amount="90", who="")
        text = re.sub(r"<[^>]+>", "", preview(**on, amount="1,090.00"))
        assert "we owe them $100.00 now" in text and "they owe us $990.00" in text
        assert "It also adds Difference (L) of $990.00, leaving their balance at 0.00" in text
        text = re.sub(r"<[^>]+>", "", preview(**on, amount="90"))
        assert "we owe them $10.00" in text and "Difference (G) of $10.00" in text
        assert 'class="money"' in preview(**on, amount="90")  # hidden with the other amounts
        assert "No Difference is needed" in preview(**on, amount="100")
        assert len(rows(settings)) == 5  # nothing added

    def test_shown_and_deleted_from_the_debt_tab(self, settings, client):
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "100", "Dad")
        store.add_debt_movement("2026-02-05", "Debt - Paid", "De", "Ca", "90", "Dad", remaining_as_difference=True)
        index = len(rows(settings)) - 1
        debt = client.get("/debt", params={"month": ""}).text
        assert "Difference (G)" in debt and f'hx-post="/debt/movements/{index}/delete"' in debt
        assert f"/debt/movements/{index}/edit" not in debt  # deleted only
        fp = row_fingerprint(rows(settings).iloc[index])
        ledger = client.get("/", params={"month": ""}).text
        assert "A Difference added with a debt movement" in ledger
        r = client.post(f"/debt/movements/{index}/delete", data={"fingerprint": fp})
        assert r.status_code == 200 and r.headers["HX-Trigger"] == "debt-changed"
        assert self.balance(settings) == pytest.approx(10)

    def test_deleted_from_the_ledger_tab(self, settings, client):
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "100", "Dad")
        store.add_debt_movement("2026-02-05", "Debt - Paid", "De", "Ca", "90", "Dad", remaining_as_difference=True)
        index = len(rows(settings)) - 1
        assert f'hx-post="/movements/{index}/delete"' in client.get("/", params={"month": ""}).text
        fp = row_fingerprint(rows(settings).iloc[index])
        assert client.post(f"/movements/{index}/delete", data={"fingerprint": fp}).status_code == 200
        assert self.balance(settings) == pytest.approx(10)

    def test_one_outside_the_debt_account_stays_in_the_ledger_tab(self, settings, client):
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write("2026-02-05;Difference (G);Ca;5.00;Eq;\n")
        index = len(rows(settings)) - 1
        ledger = client.get("/", params={"month": ""}).text
        assert f'hx-post="/movements/{index}/delete"' in ledger
        assert "A Difference added with a debt movement" not in ledger
        fp = row_fingerprint(rows(settings).iloc[index])
        assert client.post(f"/movements/{index}/delete", data={"fingerprint": fp}).status_code == 200

    def test_settles_the_results(self, settings):
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "100", "Dad")
        before = store.load().balances_today()
        store.add_debt_movement("2026-02-05", "Debt - Paid", "De", "Ca", "90", "Dad", remaining_as_difference=True)
        after = store.load().balances_today()
        assert after["De"] == pytest.approx(before["De"] - 100)
        assert after["Ca"] == pytest.approx(before["Ca"] - 90)  # the 10 not paid is a result, not money


class TestDebtSides:
    @pytest.fixture
    def settings(self, settings, tmp_path):
        path = tmp_path / "creditors.yaml"
        path.write_text("people:\n  Dad: 0\n", encoding="utf-8")
        path_yaml = settings.base_movements_path
        with_internal(path_yaml, transfers=DEBT_YAML)
        return replace(settings, creditors_path=path)

    @pytest.fixture
    def client(self, settings):
        return TestClient(create_app(settings))

    def test_add_movement(self, settings):
        store = LedgerStore(settings)
        with pytest.raises(ValueError, match=re.escape("Credit account must match: Cc != De")):
            store.add("2026-02-01", "Debt - Incurred", "Ca", "Cc", "1", debt=True)
        store.add("2026-02-01", "Debt - Incurred", "Ca", "De", "1", debt=True)
        assert rows(settings).iloc[-1]["acc_credit"] == "De"

    def test_not_in_the_ledger_tab(self, settings, client):
        with pytest.raises(ValueError, match="'Debt - Incurred' are added from the Debt tab"):
            LedgerStore(settings).add("2026-02-01", "Debt - Incurred", "Ca", "De", "1")
        with pytest.raises(ValueError, match="'Move' is not a debt base movement"):
            LedgerStore(settings).add("2026-02-01", "Move", "Ca", "De", "1", debt=True)
        assert client.post("/api/movements", json={
            "move_date": "2026-02-01", "base_movement": "Debt - Incurred", "acc_debit": "Ca", "acc_credit": "De", "amount": 1,
        }).status_code == 422
        r = client.get("/form/bases", params={"move_type": "transfer"})
        assert "Debt" not in r.text and "Loan" not in r.text and "Move" in r.text

    def test_add_debt_movement_names_who_first(self, settings):
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "10", "Dad", "for the car")
        store.add_debt_movement("2026-02-02", "Debt - Incurred", "Ca", "De", "5", "Dad", "dad's lunch")  # already named
        store.add_debt_movement("2026-02-03", "Debt - Incurred", "Ca", "De", "1", "Dad")
        assert list(rows(settings).comment[-3:]) == ["Dad: for the car", "dad's lunch", "Dad"]
        with pytest.raises(ValueError, match="'Mum' is not someone of the debt account De"):
            store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "1", "Mum")
        with pytest.raises(ValueError, match="Pick who"):
            store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "1", " ")
        de, lo = store.load().debt_accounts()
        assert {c.name: c.amount for c in de.creditors.values()} == {"Dad": pytest.approx(16)}
        assert {c.name: c.amount for c in lo.creditors.values()} == {"Dad": 0}

    def test_no_debt_movements_of_companies(self, settings, tmp_path):
        settings = replace(settings, work_path=settings.ledger_path.with_name("work.csv"))
        settings.work_path.write_text(WORK_CSV, encoding="utf-8")
        settings.creditors_path.write_text("people:\n  Dad: 0\n")
        store = LedgerStore(with_salaries(settings))
        with pytest.raises(ValueError, match="'Acme' is a company: its debt comes from the salaries of the Work tab"):
            store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "1", "Acme")
        r = TestClient(create_app(settings)).get("/debt")
        form = r.text.split('id="debt-form"')[1].split("</form>")[0]
        assert "Acme" not in form and '<optgroup label="Companies">' not in form and "Dad" in form
        assert "<strong>Acme</strong>" in r.text  # still listed with its debt

    def test_running_debt(self, settings, client):
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "20", "Dad", "")
        store.add_debt_movement("2026-02-02", "Debt - Paid", "De", "Ca", "25", "Dad", "")
        with settings.ledger_path.open("a") as f:  # naming no one: unassigned
            f.write("2026-02-03;Debt - Incurred;Ca;7;De;Who knows\n")
        moves = build_debts(store.load())["movements"]
        assert [m["debt"] for m in moves if not m["initial"]] == [pytest.approx(7), pytest.approx(-5), pytest.approx(20)]
        table = client.get("/debt", params={"month": ""}).text.split('data-sortable="debt-movements"')[1]
        assert "Debt</th>" in table and '<td class="num money balance gain" data-sort="-5.0">-$5.00</td>' in table

    def test_paid_and_collected_are_lighter(self, client):
        form = client.get("/debt").text
        assert '<label class="liability" title="Debt - Incurred">' in form
        assert '<label class="liability-light" title="Debt - Paid">' in form
        assert '<label class="asset-light" title="Loan - Collected">' in client.get("/form/debt-bases", params={"debt_kind": "Loan"}).text

    def test_debt_tab_form(self, settings, client):
        r = client.get("/debt")
        form = r.text.split('id="debt-form"')[1].split("</form>")[0]
        # The four debt base movements: first Debt or Loan, then the two of the kind picked
        assert 'name="debt_kind" value="Debt" checked' in form and 'name="debt_kind" value="Loan"' in form
        assert 'value="Debt - Incurred" checked' in form and form.count('name="base_movement"') == 2
        for label in ("Incurred", "Paid"):
            assert f"{label}\n    </label>" in form
        assert "Given" not in form and 'hx-get="/form/debt-bases"' in form
        loan = client.get("/form/debt-bases", params={"debt_kind": "Loan"}).text
        assert 'value="Loan - Given" checked' in loan and 'value="Loan - Collected"' in loan and "Incurred" not in loan
        assert '<label class="asset"' in loan and 'name="acc_debit"' in loan  # with the account fields
        # A loan base movement, e.g. of one edited, picks its kind
        fields = debt_form_fields(LedgerStore(settings).load(), "Loan - Collected", kind="Debt")
        assert fields["kind"] == "Loan" and fields["base_movement"] == "Loan - Collected"
        assert '<optgroup label="People">' in form
        assert 'hx-trigger="debt-changed from:body"' in r.text
        r = client.post("/debt/movements", data={
            "move_date": "2026-02-01", "base_movement": "Debt - Incurred", "acc_debit": "Ca", "acc_credit": "De",
            "amount": "20", "who": "Dad", "comment": "Groceries",
        })
        assert "Added Debt - Incurred (Dad): $20.00" in r.text and r.headers["HX-Trigger"] == "debt-changed"
        assert rows(settings).iloc[-1]["comment"] == "Dad: Groceries"
        r = client.post("/debt/movements", data={
            "move_date": "2026-02-01", "base_movement": "Debt - Incurred", "acc_debit": "Ca", "acc_credit": "De",
            "amount": "20", "who": "",
        })
        assert 'class="msg error"' in r.text and "Pick who" in r.text
        assert client.post("/api/debt/movements", json={
            "move_date": "2026-02-01", "base_movement": "Debt - Incurred", "acc_debit": "Ca", "acc_credit": "De",
            "amount": 1, "who": "Dad",
        }).status_code == 201

    def test_edit_debt_movement(self, settings, client):
        LedgerStore(settings).add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "20", "Dad", "Groceries")
        assert 'hx-get="/debt/movements/4/edit"' in client.get("/debt", params={"month": ""}).text
        fp = row_fingerprint(rows(settings).iloc[4])
        r = client.get("/debt/movements/4/edit", params={"fingerprint": fp})
        assert "Edit debt movement" in r.text and 'hx-post="/debt/movements/4/edit"' in r.text
        assert "<option selected>Dad</option>" in r.text and 'name="comment" value="Groceries"' in r.text
        r = client.post("/debt/movements/4/edit", data={
            "fingerprint": fp, "move_date": "2026-02-02", "base_movement": "Debt - Incurred", "acc_debit": "Ca",
            "acc_credit": "De", "amount": "25", "who": "Dad", "comment": "Dinner",
        })
        assert "Updated L-5 Debt - Incurred (Dad): $25.00" in r.text and r.headers["HX-Trigger"] == "debt-changed"
        assert "New debt movement" in r.text
        assert rows(settings).iloc[4][["move_date", "amount", "comment"]].tolist() == ["2026-02-02", "25.00", "Dad: Dinner"]
        # Only the debt movements are edited from the Debt tab, and they aren't from the Ledger one
        with pytest.raises(ValueError, match="'Food' is not a debt base movement"):
            LedgerStore(settings).edit(2, None, "2026-01-03", "Food", "Eq", "Ca", "1", debt=True)
        with pytest.raises(ValueError, match="'Debt - Incurred' are edited from the Debt tab"):
            LedgerStore(settings).edit(4, None, "2026-02-02", "Debt - Incurred", "Ca", "De", "1", "Dad")

    def test_delete_debt_movement(self, settings, client):
        LedgerStore(settings).add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "20", "Dad")
        assert 'hx-post="/debt/movements/4/delete"' in client.get("/debt", params={"month": ""}).text
        fp = row_fingerprint(rows(settings).iloc[4])
        # Not from the Ledger tab, which links to the Debt tab instead
        assert client.post("/movements/4/delete", data={"fingerprint": fp}).status_code == 422
        assert client.delete("/api/movements/4").status_code == 422
        r = client.post("/debt/movements/4/delete", data={"fingerprint": fp})
        assert r.status_code == 200 and r.headers["HX-Trigger"] == "debt-changed"
        assert len(rows(settings)) == 4
        LedgerStore(settings).add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "20", "Dad")
        assert client.delete("/api/debt/movements/4").status_code == 200 and len(rows(settings)) == 4

    def test_who_filter(self, settings, client):
        settings.creditors_path.write_text("people:\n  Dad: 0\n  Mum: 0\n", encoding="utf-8")
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "20", "Dad")
        store.add_debt_movement("2026-02-02", "Loan - Given", "Lo", "Ca", "5", "Mum")
        with settings.ledger_path.open("a") as f:
            f.write("2026-02-03;Debt - Paid;De;1;Ca;no one\n")
        data = build_debts(store.load(), "", "Mum")
        assert [m["who"] for m in data["movements"]] == ["Mum"] and data["who"] == "Mum"
        assert [m["comment"] for m in build_debts(store.load(), "", "-")["movements"]] == ["no one"]
        assert len(build_debts(store.load(), "", "Nobody")["movements"]) == 3  # unknown: everyone
        r = client.get("/debt", params={"month": "", "who": "Dad"})
        assert '<form id="who-filter"' in r.text and "<option selected>Dad</option>" in r.text
        assert "<h2>Movements <small>1, all time</small></h2>" in r.text

    def test_debt_tab_form_without_debt_base_movements(self, settings, client):
        path = settings.base_movements_path
        path.write_text(path.read_text().replace("    Loan - Collected:\n      description: d\n      credit: Lo\n", ""))
        form = client.get("/debt").text.split('id="debt-form"')[1].split("</form>")[0]
        assert "The Debt tab needs these base movements" in form and "&#39;Loan - Collected&#39;" in form
        assert form.count('name="base_movement"') == 2  # those of Debt, picked first
        loan = client.get("/form/debt-bases", params={"debt_kind": "Loan"}).text
        assert loan.count('name="base_movement"') == 1 and 'value="Loan - Given" checked' in loan

    def test_internal_ones_link_to_their_row_in_the_debt_tab(self, settings, client):
        with settings.ledger_path.open("a") as f:
            f.write("2026-02-03;Debt - Paid;De;5;Ca;\n")
        LedgerStore(settings).freeze_ids()
        # To the Debt tab in the month of the movement, so it isn't filtered out
        assert 'href="/debt?month=2026-02#movement-L-5" title="Added, edited and deleted from the Debt tab"' in client.get("/").text
        assert '<tr id="movement-L-5">' in client.get("/debt", params={"month": "2026-02"}).text

    def test_form_offers_only_debt_accounts(self, client):
        r = client.get("/form/accounts", params={"base_movement": "Debt - Incurred"})
        debit, credit = r.text.split("<span>Credit")
        assert '<input value="De" readonly' in credit  # always De
        assert "<option >Cc</option>" not in credit and "<option >Cc</option>" in debit

    def test_add_base_movement(self, settings, client):
        LedgerStore(settings).add_base_movement("Repay", "transfer", "d", debit="De")
        bases = {b["name"]: b for b in client.get("/api/base-movements").json()}
        assert (bases["Debt - Incurred"]["debit"], bases["Debt - Incurred"]["credit"]) == (None, "De")
        assert (bases["Repay"]["debit"], bases["Repay"]["credit"]) == ("De", None)
        r = client.get("/form/base-sides", params={"move_type": "transfer"})
        assert 'value="DEBT"' not in r.text and "Any debt account" not in r.text

    def test_accounts_special_column(self, settings, tmp_path):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace("liability:\n", "  Br:\n    description: Broker\nliability:\n"), encoding="utf-8"
        )
        path = tmp_path / "investment.yaml"
        path.write_text("Br:\n  investments:\n    T: stock\n", encoding="utf-8")
        r = TestClient(create_app(replace(settings, investments_path=path))).get("/accounts")
        assert "<th>Liquid</th><th>Special</th>" in r.text
        specials = {
            row.split("<strong>")[1].split("</strong>")[0]: row.split("</td>")[4].split(">")[-1]
            for row in re.split(r'<tr id="account-[^"]+">', r.text.split("<tbody>", 1)[1])[1:]
            if "<strong>" in row
        }
        assert specials == {"Ca": "-", "Bk": "-", "Br": "Investment", "Cc": "-", "Fi": "-", "Lo": "Debt", "De": "Debt", "Eq": "-"}


class TestDebtAccounts:
    def test_debt_accounts_are_loaded(self, settings, tmp_path):
        path = tmp_path / "creditors.yaml"
        path.write_text("people:\n  Dad: 10\n", encoding="utf-8")
        accounts = LedgerStore(replace(settings, creditors_path=path)).load().accounts
        assert accounts["De"].creditors["Dad"].is_creditor
        assert accounts["De"].initial_amount == 10.0


class TestDebtPage:
    @pytest.fixture
    def settings(self, settings, tmp_path):
        path = tmp_path / "creditors.yaml"
        path.write_text(
            "people:\n  Dad: 100\n  Ana: -30\n  Leo: 0\nservices:\n  Bank: 50.5\n",
            encoding="utf-8",
        )
        return replace(settings, creditors_path=path)

    @pytest.fixture
    def client(self, settings):
        return TestClient(create_app(settings))

    def test_debt(self, client):
        r = client.get("/debt")
        assert r.status_code == 200
        assert 'href="/debt" aria-current="page"' in r.text
        assert card_labels(r.text) == ["We owe", "Owed to us", "Net debt"]
        cards = r.text.split('<div class="cards">')[1].split("</div>\n  </div>")[0]
        assert "$150.50" in cards and "$30.00" in cards and "$120.50" in cards
        assert 'data-sortable="debts-people"' in r.text and 'data-sortable="debts-services"' in r.text
        for name, role in (("Dad", "Creditor"), ("Ana", "Debtor"), ("Leo", "Settled")):
            assert f"<strong>{name}</strong></td>\n        <td>{role}</td>" in r.text

    def test_month_filter(self, settings, client):
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write("2026-01-05;Food;Eq;20;De;dad\n2026-02-05;Food;Eq;30;De;Dad\n")
        snapshot = LedgerStore(settings).load()
        amounts = {
            month: snapshot.debt_accounts(month)[0].creditors["Dad"].amount
            for month in ("", "2025-12", "2026-01", "2026-02")
        }
        assert amounts == {"": 150, "2025-12": 100, "2026-01": 120, "2026-02": 150}
        assert snapshot.accounts["De"].creditors["Dad"].amount == 100  # left as loaded
        r = client.get("/debt", params={"month": "2026-01"})
        assert '<option value="2026-01" selected>' in r.text
        assert "by people, services and companies, as of end of 2026-01</small>" in r.text
        cards = r.text.split('<div class="cards">')[1].split("</div>\n  </div>")[0]
        assert "$170.50" in cards  # Dad 120 and Bank 50.50
        # The month is shared with the other tabs
        assert '<option value="2026-01" selected>' in client.get("/accounts").text
        client.get("/results", params={"month": "2025-12"})
        assert '<option value="2025-12" selected>' in client.get("/debt").text
        r = client.get("/debt", params={"month": "2026-02"}, headers=HTMX)
        assert "<html" not in r.text and '<div class="view">' in r.text and "$200.50" in r.text

    def test_movements(self, settings, client):
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write("2026-01-05;Food;Eq;20;De;dad\n2026-02-05;Food;Eq;30;De;no one\n")
        LedgerStore(settings).freeze_ids()
        r = client.get("/debt", params={"month": ""})
        movements = r.text.split('data-sortable="debt-movements"')[1].split("</table>")[0]
        # With their ids, the same as in the Ledger tab
        assert movement_ids(movements) == ["L-6", "L-5", "A-De", "A-Lo"]
        assert set(movement_ids(movements)) <= set(movement_ids(client.get("/", params={"initial": True}).text))
        # The initial movements too, with the initial amounts of everyone: what we owe in De, what we are owed in Lo
        assert "<h2>Movements <small>4, all time</small></h2>" in r.text
        assert movements.index("2026-02-05") < movements.index("2026-01-05") < movements.index("Initial (L)")
        assert "<strong>Dad</strong>" in movements and 'title="The comment names no one: unassigned debt">-' in movements
        assert ">Initial amounts</td>" in movements and 'data-sort="150.5"' in movements and 'data-sort="30.0"' in movements
        assert "Salary" not in movements  # not of a debt account
        r = client.get("/debt", params={"month": "2026-02"})
        assert "<h2>Movements <small>1, in 2026-02</small></h2>" in r.text

    def test_initial_amounts_count_from_the_initial_date(self, settings):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace("{description: Debts}", "{description: Debts, initial_date: 2026-01-02}"),
            encoding="utf-8",
        )
        snapshot = LedgerStore(settings).load()
        assert snapshot.debt_accounts("2025-12")[0].creditors["Dad"].amount == 0
        assert snapshot.debt_accounts("2026-01")[0].creditors["Dad"].amount == 100

    def test_no_alerts_when_all_is_as_expected(self, client):
        r = client.get("/debt")
        assert 'class="msg warn alert"' not in r.text
        assert '<td class="muted">-</td>' in r.text and "<td class=\"\">Creditor</td>" in r.text  # Dad, Bank

    def test_debt_bars_show_positive_amounts(self, client):
        r = client.get("/debt")
        owed = r.text.split("Owed to us <span")[1].split("<h2>👥 People")[0]
        assert '<span class="bar-name">Ana</span>' in owed and "-$30.00</span>" not in owed
        we_owe = r.text.split("We owe <span")[1].split("Owed to us <span")[0]
        assert '<span class="bar-name">Dad</span>' in we_owe
        assert "Leo" not in we_owe and "Ana" not in we_owe

    def test_ledger_debts_go_to_the_people_in_the_comment(self, settings, client):
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write(
                "2026-01-05;Food;Eq;20;De;Fee for dad\n"  # a credit: we owe more
                '2026-01-06;Move;De;"1,040.5";Ca;bank 2026-01\n'  # a debit: we owe less
                "2026-01-07;Move;De;5;Ca;Zurich 1/24\n"  # names no one
                "2026-01-08;Move;Ca;7;Bk;Dad\n"  # no debt account
            )
        settings.ledger_path.write_text(
            settings.ledger_path.read_text().replace("2026-01-04;Gift;Ca;10;", "2026-01-04;Gift;Ca;2000;")
        )
        cc, lo = LedgerStore(settings).load().debt_accounts()
        # The initial amounts are only counted once, from the creditors YAML: Ana owes us, in Lo
        assert {n: c.amount for n, c in cc.creditors.items()} == {"Dad": 120, "Ana": 0, "Leo": 0, "Bank": -990}
        assert {n: c.amount for n, c in lo.creditors.items()} == {"Dad": 0, "Ana": -30, "Leo": 0}
        assert cc.unassigned == -5
        r = client.get("/debt")
        assert card_labels(r.text) == ["We owe", "Owed to us", "Unassigned", "Net debt"]
        cards = r.text.split('<div class="cards">')[1].split("</div>\n  </div>")[0]
        assert "$120.00" in cards and "$1,020.00" in cards and "-$5.00" in cards and "-$905.00" in cards
        accounts = r.text.split("<h2>Debt accounts</h2>")[1]
        assert 'data-sort="-5.0">-$5.00</td>' in accounts
        # Bank is a service, expected to be a creditor
        assert '<strong>Bank</strong></td>\n        <td class="nowrap hint warn" title="Expected to be a creditor">⚠ Debtor</td>' in r.text
        alerts = r.text.split('<details class="msg warn alert" role="alert">')[1:]
        assert len(alerts) == 2
        assert "Unassigned debt of" in alerts[0] and "<li><span class=\"money\">Zurich 1/24</span>: <span class=\"money\">-$5.00</span></li>" in alerts[0]
        assert "1 is not in the expected role" in alerts[1] and "<strong>Bank</strong>" in alerts[1]
        assert "is a debtor (" in alerts[1] and "but is expected to be a creditor" in alerts[1]


class TestDebtKinds:
    @pytest.fixture
    def settings(self, settings, tmp_path):
        creditors = tmp_path / "creditors.yaml"
        creditors.write_text(
            "people:\n  Dad: 100\nservices:\n  Gas: 20\n",
            encoding="utf-8",
        )
        work = tmp_path / "work.csv"
        work.write_text(WORK_CSV, encoding="utf-8")
        return replace(with_salaries(settings), creditors_path=creditors, work_path=work)

    def test_companies_come_from_the_work_history(self, settings):
        before = settings.creditors_path.read_text(encoding="utf-8")
        accounts = LedgerStore(settings).load().accounts
        cc, lo = accounts["De"], accounts["Lo"]
        assert {n: (c.kind.value, c.amount) for n, c in cc.creditors.items()} == {
            "Dad": ("people", 100), "Gas": ("services", 20),
        }
        # The companies are expected to be debtors: only in Lo
        assert {n: (c.kind.value, c.amount) for n, c in lo.creditors.items()} == {
            "Dad": ("people", 0), "Acme": ("companies", 0), "Globex": ("companies", 0),
        }
        assert cc.initial_amount == 120 and lo.initial_amount == 0
        assert settings.creditors_path.read_text(encoding="utf-8") == before  # never written

    def test_new_companies_of_the_work_history(self, settings):
        store = LedgerStore(settings)
        store.save_month("2026-02", "Initech", "Dev", "10", {"Tax": "2"})
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write("2026-02-05;Food;Eq;7;Lo;initech bonus\n")
        assert store.load().debt_accounts()[1].creditors["Initech"].amount == 7

    def test_a_company_cannot_be_another_kind(self, settings):
        text = settings.creditors_path.read_text(encoding="utf-8")
        settings.creditors_path.write_text(text.replace("  Gas: 20\n", "  Gas: 20\n  Globex: 1\n"), encoding="utf-8")
        with pytest.raises(LedgerDataError, match="'Globex' is a company of the work history"):
            LedgerStore(settings).load()

    def test_debt_page_by_kind(self, settings):
        r = TestClient(create_app(with_salaries(settings, debit="Lo"))).get("/debt")  # Globex owes us the salaries
        assert r.status_code == 200
        for title, count in (("👥 People", 1), ("🧾 Services", 1), ("🏢 Companies", 2)):
            assert f"<h2>{title} <small>{count}</small></h2>" in r.text
        companies = r.text.split("<h2>🏢 Companies")[1].split("</table>")[0]
        assert "<strong>Acme</strong></td>\n        <td>Settled</td>" in companies
        assert "<strong>Dad</strong>" not in companies
        we_owe = r.text.split("We owe <span")[1].split("Owed to us <span")[0]
        assert '<span class="bar-name">Services</span>' in we_owe and '<span class="bar-name">Gas</span>' in we_owe
        owed = r.text.split("Owed to us <span")[1].split("<h2>👥 People")[0]
        assert '<span class="bar-name">Companies</span>' in owed and "People</span>" not in owed


class TestDebtsAndLoans:
    """De has the debts (what we owe) and Lo the loans (what we are owed), sharing who they are about."""

    @pytest.fixture
    def settings(self, settings, tmp_path):
        path = tmp_path / "creditors.yaml"
        path.write_text(
            "people:\n  Dad: 0\n  Ann:\n    amount: 0\n    expected: creditor\n  Bob:\n    amount: 0\n    expected: debtor\n",
            encoding="utf-8",
        )
        with_internal(settings.base_movements_path, DIFFERENCE_YAML, DEBT_YAML)
        return replace(settings, creditors_path=path)

    @pytest.fixture
    def client(self, settings):
        return TestClient(create_app(settings))

    def test_each_kind_offers_who_can_be_in_its_account(self, settings, client):
        snapshot = LedgerStore(settings).load()
        assert debt_form_fields(snapshot, "Debt - Paid")["who"] == {"People": ["Ann", "Dad"]}
        assert debt_form_fields(snapshot, "Loan - Given")["who"] == {"People": ["Bob", "Dad"]}
        assert debt_form_fields(snapshot, "Loan - Given")["account"] == "Lo"
        # Swapping the kind keeps who was picked, if they can be in its account
        loan = client.get("/form/debt-bases", params={"debt_kind": "Loan", "who": "Dad"}).text
        assert "<option selected>Dad</option>" in loan and "Ann" not in loan and "(of Lo)" in loan
        loan = client.get("/form/debt-bases", params={"debt_kind": "Loan", "who": "Ann"}).text
        assert "<option selected>" not in loan

    def test_expected_roles_only_move_their_account(self, settings):
        store = LedgerStore(settings)
        with pytest.raises(ValueError, match="'Ann' is expected to be a creditor: their movements are in De, not Lo"):
            store.add_debt_movement("2026-02-01", "Loan - Given", "Lo", "Ca", "10", "Ann")
        with pytest.raises(ValueError, match="'Bob' is expected to be a debtor: their movements are in Lo, not De"):
            store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "10", "Bob")
        with pytest.raises(ValueError, match="Debit account must match: De != Lo"):
            store.add_debt_movement("2026-02-01", "Loan - Given", "De", "Ca", "10", "Dad")

    def test_who_has_no_expected_role_moves_both(self, settings, client):
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "100", "Dad")
        store.add_debt_movement("2026-02-02", "Loan - Given", "Lo", "Ca", "30", "Dad")
        store.add_debt_movement("2026-02-03", "Loan - Given", "Lo", "Ca", "5", "Bob")
        balances = store.load().balances_today()
        assert (balances["De"], balances["Lo"]) == (100, 35)  # a liability and an asset
        r = client.get("/debt")
        people = r.text.split("<h2>👥 People")[1].split("</table>")[0]
        assert "<th class=\"num\" title=\"What we owe them in De (Debts)\">De</th>" in people
        dad = people.split("<strong>Dad</strong>")[1].split("</tr>")[0]
        # What we owe Dad in De, what he owes us in Lo, and what we owe him in all
        assert 'data-sort="100.0">$100.00' in dad and 'data-sort="-30.0">-$30.00' in dad and "<strong>$70.00</strong>" in dad
        bob = people.split("<strong>Bob</strong>")[1].split("</tr>")[0]
        assert 'title="Not in this account: expected to be a debtor">-</td>' in bob
        cards = r.text.split('<div class="cards">')[1].split("</div>\n  </div>")[0]
        assert "$70.00" in cards and "$5.00" in cards and "$65.00" in cards  # we owe, owed to us, net
        # The remaining with Dad is that of the account of the movement
        assert store.add_debt_movement(
            "2026-02-04", "Loan - Collected", "Ca", "Lo", "20", "Dad", remaining_as_difference=True,
        ) == ("Difference (L)", 10.0)
        last = rows(settings).iloc[-1]
        assert (last.acc_debit, last.acc_credit) == ("Eq", "Lo")

    def test_movements_naming_someone_of_the_other_account(self, settings, client):
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write("2026-01-05;Food;Eq;20;De;Bob's dinner\n2026-01-06;Food;Eq;5;De;Dad\n")
        r = client.get("/debt")
        alerts = r.text.split('<details class="msg warn alert" role="alert">')[1:]
        misplaced = next(a for a in alerts if "isn't in its debt account" in a).split("</details>")[0]
        assert "1 movement names" in misplaced and "names <strong>Bob</strong>" in misplaced and "of <code>Lo</code>" in misplaced
        assert "Dad" not in misplaced
