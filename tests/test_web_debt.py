"""Tests for the Debt tab and the debt account."""

import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import (
    DEBT_YAML,
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


class TestDebtSides:
    @pytest.fixture
    def settings(self, settings, tmp_path):
        path = tmp_path / "creditors.yaml"
        path.write_text("De:\n  people:\n    Dad: 0\n", encoding="utf-8")
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
        debts = {c.name: c.amount for a in store.load().debt_accounts() for c in a.creditors.values()}
        assert debts == {"Dad": pytest.approx(16)}

    def test_no_debt_movements_of_companies(self, settings, tmp_path):
        settings = replace(settings, work_path=settings.ledger_path.with_name("work.csv"))
        settings.work_path.write_text(WORK_CSV, encoding="utf-8")
        settings.creditors_path.write_text("De:\n  people:\n    Dad: 0\n")
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
        settings.creditors_path.write_text("De:\n  people:\n    Dad: 0\n    Mum: 0\n", encoding="utf-8")
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "20", "Dad")
        store.add_debt_movement("2026-02-02", "Loan - Given", "De", "Ca", "5", "Mum")
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
        path.write_text(path.read_text().replace("    Loan - Collected:\n      description: d\n      credit: De\n", ""))
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
        path.write_text("Br:\n  T: stock\n", encoding="utf-8")
        r = TestClient(create_app(replace(settings, investments_path=path))).get("/accounts")
        assert "<th>Liquid</th><th>Special</th>" in r.text
        specials = {
            row.split("<strong>")[1].split("</strong>")[0]: row.split("</td>")[4].split(">")[-1]
            for row in re.split(r'<tr id="account-[^"]+">', r.text.split("<tbody>", 1)[1])[1:]
            if "<strong>" in row
        }
        assert specials == {"Ca": "-", "Bk": "-", "Br": "Investment", "Cc": "-", "Fi": "-", "De": "Debt", "Eq": "-"}


class TestDebtAccounts:
    def test_debt_accounts_are_loaded(self, settings, tmp_path):
        path = tmp_path / "creditors.yaml"
        path.write_text("De:\n  people:\n    Dad: 10\n", encoding="utf-8")
        accounts = LedgerStore(replace(settings, creditors_path=path)).load().accounts
        assert accounts["De"].creditors["Dad"].is_creditor
        assert accounts["De"].initial_amount == 10.0


class TestDebtPage:
    @pytest.fixture
    def settings(self, settings, tmp_path):
        path = tmp_path / "creditors.yaml"
        path.write_text(
            "De:\n  people:\n    Dad: 100\n    Ana: -30\n    Leo: 0\n  services:\n    Bank: 50.5\n",
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
        assert movement_ids(movements) == ["L-6", "L-5", "A-De"]
        assert set(movement_ids(movements)) <= set(movement_ids(client.get("/", params={"initial": True}).text))
        # The initial movement too, with the initial amounts of everyone
        assert "<h2>Movements <small>3, all time</small></h2>" in r.text
        assert movements.index("2026-02-05") < movements.index("2026-01-05") < movements.index("Initial (L)")
        assert "<strong>Dad</strong>" in movements and 'title="The comment names no one: unassigned debt">-' in movements
        assert ">Initial amounts</td>" in movements and 'data-sort="120.5"' in movements
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
        [cc] = LedgerStore(settings).load().debt_accounts()
        # The initial amounts are only counted once, from the creditors YAML
        assert {n: c.amount for n, c in cc.creditors.items()} == {"Dad": 120, "Bank": -990, "Ana": -30, "Leo": 0}
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
        assert "Unassigned debt of" in alerts[0] and "<li>Zurich 1/24: <span class=\"money\">-$5.00</span></li>" in alerts[0]
        assert "1 is not in the expected role" in alerts[1] and "<strong>Bank</strong>" in alerts[1]
        assert "is a debtor (" in alerts[1] and "but is expected to be a creditor" in alerts[1]


class TestDebtKinds:
    @pytest.fixture
    def settings(self, settings, tmp_path):
        creditors = tmp_path / "creditors.yaml"
        creditors.write_text(
            "De:\n  people:\n    Dad: 100\n  services:\n    Gas: 20\n",
            encoding="utf-8",
        )
        work = tmp_path / "work.csv"
        work.write_text(WORK_CSV, encoding="utf-8")
        return replace(with_salaries(settings), creditors_path=creditors, work_path=work)

    def test_companies_come_from_the_work_history(self, settings):
        before = settings.creditors_path.read_text(encoding="utf-8")
        cc = LedgerStore(settings).load().accounts["De"]
        assert {n: (c.kind.value, c.amount) for n, c in cc.creditors.items()} == {
            "Dad": ("people", 100), "Gas": ("services", 20),
            "Acme": ("companies", 0), "Globex": ("companies", 0),
        }
        assert cc.initial_amount == 120
        assert settings.creditors_path.read_text(encoding="utf-8") == before  # never written

    def test_new_companies_of_the_work_history(self, settings):
        store = LedgerStore(settings)
        store.save_month("2026-02", "Initech", "Dev", "10", {"Tax": "2"})
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write("2026-02-05;Food;Eq;7;De;initech bonus\n")
        assert store.load().debt_accounts()[0].creditors["Initech"].amount == 7

    def test_a_company_cannot_be_another_kind(self, settings):
        text = settings.creditors_path.read_text(encoding="utf-8")
        settings.creditors_path.write_text(text.replace("    Gas: 20\n", "    Gas: 20\n    Globex: 1\n"), encoding="utf-8")
        with pytest.raises(LedgerDataError, match="'Globex' of the account 'De' is a company of the work history"):
            LedgerStore(settings).load()

    def test_debt_page_by_kind(self, settings):
        r = TestClient(create_app(with_salaries(settings, debit="De"))).get("/debt")  # Globex owes us the salaries
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
