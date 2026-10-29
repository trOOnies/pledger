"""Tests for the Work tab and the salaries generated from it."""

import datetime as dt
import html
import json
import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import HTMX, WORK_CSV, card_labels, with_salaries

from pledger.web.app import create_app
from pledger.web.store import (
    LedgerDataError,
    LedgerStore,
    deduction_id,
    work_fingerprint,
)


class TestWork:
    @pytest.fixture
    def settings(self, settings, tmp_path):
        path = tmp_path / "work.csv"
        path.write_text(WORK_CSV, encoding="utf-8")
        return replace(settings, work_path=path)

    @pytest.fixture
    def store(self, settings):
        return LedgerStore(settings)

    @pytest.fixture
    def client(self, settings):
        return TestClient(create_app(settings))

    def text(self, settings):
        return settings.work_path.read_text(encoding="utf-8")

    def test_load(self, store):
        work = store.load_work()
        assert [m.month for m in work.months] == ["2025-10", "2025-11", "2025-12", "2026-01"]
        assert work.months[2].is_gap
        assert list(work.companies) == ["Globex", "Acme"]
        assert work.current.position == "GX Dev"
        assert work.position_of(work.months[1]).name == "Acme Sr"

    def test_no_current_job_after_a_gap(self, store, settings):
        settings.work_path.write_text(WORK_CSV + "\n2026-02;;;;", encoding="utf-8")
        assert store.load_work().current is None

    @pytest.mark.parametrize(
        ("line", "error"),
        [
            ("2026-03;;;;", "2026-03 comes after 2026-01, but 2026-02 is expected"),
            ("2026-01;;;;", "is expected"),
            ("2026-02;Globex;;1;1", "both a company and a job position"),
            ("2026-02;;;1;1", "cannot have a salary"),
            ("2026-02;;;;1", "cannot have a salary"),
            ("2026-02;Globex;GX Dev;;", "must have a gross salary"),
            ("2026-02;Globex;GX Dev;1;2", "deductions of 2026-02 cannot be over its gross salary"),
            ("2026-02;Globex;GX Dev;1;x", "Invalid deduction 'Tax' of 2026-02"),
        ],
    )
    def test_data_errors(self, store, settings, line, error):
        settings.work_path.write_text(WORK_CSV + "\n" + line, encoding="utf-8")
        with pytest.raises(LedgerDataError, match=error):
            store.load_work()

    def test_deductions(self, store):
        work = store.load_work()
        assert work.deductions == ["Tax"]
        acme = work.months[1]
        assert (acme.gross, acme.deductions, acme.net) == (1500.5, {"Tax": 300.5}, 1200)
        assert work.months[2].net is None  # a gap

    def test_missing_columns(self, store, settings):
        settings.work_path.write_text("Month;Company\n2026-01;;\n", encoding="utf-8")
        with pytest.raises(LedgerDataError, match="missing the columns"):
            store.load_work()

    def test_add_after_the_last_month_keeps_the_file(self, store, settings):
        assert store.save_month("2026-02", "Globex", "GX Dev", "2,100", {"Tax": "400"}) is True
        assert self.text(settings) == WORK_CSV + "\n2026-02;Globex;GX Dev;2100.00;400.00"
        assert store.save_month("2026-03") is True  # a gap
        assert self.text(settings).endswith("\n2026-03;;;;")
        assert store.save_month("2026-04", "Globex", "GX Dev", "1") is True  # deductions left out are 0
        assert self.text(settings).endswith("\n2026-04;Globex;GX Dev;1.00;0.00")

    def test_add_before_the_first_month(self, store, settings):
        store.save_month("2025-09", "Acme", "Acme Jr", "900", {"Tax": "200"})
        assert self.text(settings).startswith(
            "Month;Company;Position;Work - Salary Main;Tax\n2025-09;Acme;Acme Jr;900.00;200.00\n2025-10;"
        )

    def test_existing_months_are_replaced(self, store, settings):
        assert store.save_month("2025-12", "Acme", "Acme Sr", "1500.5", {"Tax": "300.5"}) is False
        assert "\n2025-12;Acme;Acme Sr;1500.50;300.50\n" in self.text(settings)
        assert store.save_month("2025-11") is False  # now a gap
        assert "\n2025-11;;;;\n" in self.text(settings)
        assert len(store.load_work().months) == 4

    def test_payment_date_adds_its_column(self, store, settings):
        store.save_month("2025-11", "Acme", "Acme Sr", "1500.5", {"Tax": "300.5"}, "2025-12-05")
        assert self.text(settings) == (
            "Month;Company;Position;Work - Salary Main;Tax;Received payment date\n"
            "2025-10;Acme;Acme Jr;1000.00;200.00;\n"
            "2025-11;Acme;Acme Sr;1500.50;300.50;2025-12-05\n"
            "2025-12;;;;;\n"
            "2026-01;Globex;GX Dev;2000.00;400.00;"
        )
        assert store.load_work().months[1].payment_date == dt.date(2025, 12, 5)
        store.save_month("2026-02", "Globex", "GX Dev", "1", {"Tax": "1"})
        assert self.text(settings).endswith("\n2026-02;Globex;GX Dev;1.00;1.00;")

    @pytest.mark.parametrize(
        ("args", "error"),
        [
            (("2026-03",), "Months cannot be skipped: add 2026-02 \\(after the last month\\) first"),
            (("2025-08",), "Months cannot be skipped"),
            (("2026-02", "Globex", "", "1", {"Tax": "1"}), "both a company and a job position"),
            (("2026-02", "", "", "1"), "cannot have a salary"),
            (("2026-02", "", "", "", {"Tax": "1"}), "cannot have a salary"),
            (("2026-02", "Globex", "GX Dev", "", {"Tax": "1"}), "must have a gross salary"),
            (("2026-02", "Globex", "GX Dev", "1", {"Tax": "2"}), "cannot be over its gross salary"),
            (("2026-02", "Globex", "GX Dev", "1", {"Tax": "-1"}), "cannot be negative"),
            (("2026-02", "Globex", "GX Dev", "1", {"Tax": "x"}), "Invalid deduction 'Tax'"),
            (("2026-02", "Globex", "GX Dev", "1", {"Month": "1"}), "Invalid deduction of the salary: 'Month'"),
            (("2026-02", "Globex", "GX Dev", "1", {"A;B": "1"}), "Invalid deduction of the salary"),
            (("2026-02", "Globex", "GX Dev", "1", {"Tax": "1"}, "2026-02-30"), "Invalid received payment date"),
            (("June",), "must be YYYY-MM"),
        ],
    )
    def test_save_errors(self, store, settings, args, error):
        with pytest.raises(ValueError, match=error):
            store.save_month(*args)
        assert self.text(settings) == WORK_CSV

    def test_delete_first_or_last_month_only(self, store, settings):
        rows = store.load_work().rows
        with pytest.raises(ValueError, match="Only the first or last month"):
            store.delete_month(1, work_fingerprint(rows.iloc[1]))
        store.delete_month(3, work_fingerprint(rows.iloc[3]))
        store.delete_month(0, work_fingerprint(rows.iloc[0]))
        assert [m.month for m in store.load_work().months] == ["2025-11", "2025-12"]

    def test_page(self, client):
        r = client.get("/work")
        assert r.status_code == 200
        assert 'href="/work" aria-current="page"' in r.text
        assert card_labels(r.text) == ["Current job", "Gross", "Net"]
        assert "<strong>Globex</strong> <span class=\"badge gain\">Current</span>" in r.text
        salaries = r.text.split('data-sortable="salaries"')[1]
        assert re.findall(r"<strong>(\d{4}-\d{2})</strong>", salaries) == ["2026-01", "2025-12", "2025-11", "2025-10"]
        assert '<td class="muted" colspan="2">No job</td>' in salaries
        assert "$1,500.50" in salaries
        # Gross, deductions (itemized on hover) and net
        assert '<td class="num money " data-sort="300.5"\n            title="Tax: $300.50">$300.50</td>' in salaries
        assert '<td class="num money" data-sort="1200.0">$1,200.00</td>' in salaries
        # Only the first and last months have a delete button
        assert salaries.count('aria-label="Delete') == 2
        # The form is ready for the next month, at the last job
        assert "<h2>New month</h2>" in r.text
        assert 'name="month" value="2026-02"' in r.text and 'name="position" value="GX Dev"' in r.text

    def test_unpaid_months_warning(self, client, store, settings):
        # Worked months already over: 2025-10, 2025-11 and 2026-01 (2025-12 is a gap)
        r = client.get("/work")
        assert "⚠ 3 past months with a job have no received payment date: 2026-01, 2025-11, 2025-10." in r.text
        assert r.text.count("⚠ Not received") == 3
        store.save_month("2025-11", "Acme", "Acme Sr", "1500.5", {"Tax": "300.5"}, "2025-12-05")
        store.save_month("2026-01", "Globex", "GX Dev", "2000", {"Tax": "400"}, "2026-02-05")
        r = client.get("/work")
        assert "⚠ 1 past month with a job has no received payment date: 2025-10." in r.text
        store.save_month("2025-10", "Acme", "Acme Jr", "1000", {"Tax": "200"}, "2025-11-05")
        assert "no received payment date" not in client.get("/work").text

    def test_current_month_is_not_unpaid(self, client, store, settings):
        today = dt.date.today()
        settings.work_path.write_text(
            "Month;Company;Position;Work - Salary Main;Received payment date\n"
            f"{today:%Y-%m};Acme;Dev;10;\n",
            encoding="utf-8",
        )
        r = client.get("/work")
        assert "no received payment date" not in r.text and "⚠ Not received" not in r.text

    def test_edit_form(self, client):
        r = client.get("/work/form", params={"month": "2025-11"})
        assert "<h2>Edit 2025-11</h2>" in r.text and 'value="1,500.50"' in r.text
        assert 'name="deduction:Tax" inputmode="decimal" value="300.50"' in r.text
        assert "Save 2025-11" in r.text
        r = client.get("/work/form", params={"month": "2026-02"})
        assert "<h2>New month</h2>" in r.text

    def test_forms(self, client, store):
        r = client.post("/work/months", data={"month": "2026-03"}, headers=HTMX)
        assert "Months cannot be skipped" in r.text and 'value="2026-03"' in r.text
        r = client.post(
            "/work/months",
            data={"month": "2026-02", "company": "Globex", "position": "GX Dev", "gross": "2,000.00", "deduction:Tax": "400"},
            headers=HTMX,
        )
        assert "Added 2026-02 (GX Dev at Globex)" in r.text
        assert store.load_work().months[-1].deductions == {"Tax": 400}
        r = client.post(
            "/work/months",
            data={"month": "2026-03", "company": "Globex", "position": "GX Dev", "gross": "1", "deduction:Tax": "2"},
            headers=HTMX,
        )
        assert "cannot be over its gross salary" in r.text and 'name="deduction:Tax" inputmode="decimal" value="2"' in r.text
        assert 'name="month" value="2026-03"' in r.text  # ready for the next one
        r = client.post("/work/months", data={"month": "2025-12"}, headers=HTMX)
        assert "Updated 2025-12 (no job)" in r.text
        fingerprint = work_fingerprint(store.load_work().rows.iloc[1])
        r = client.post("/work/months/1/delete", data={"fingerprint": fingerprint}, headers=HTMX)
        assert r.status_code == 422
        r = client.post("/work/months/0/delete", data={"fingerprint": "stale"}, headers=HTMX)
        assert r.status_code == 409

    def test_api(self, client):
        acme = client.get("/api/work/companies").json()[1]
        assert acme == {
            "name": "Acme", "start": "2025-10", "end": "2025-11",
            "positions": [
                {"name": "Acme Sr", "start": "2025-11", "end": "2025-11", "months": 1},
                {"name": "Acme Jr", "start": "2025-10", "end": "2025-10", "months": 1},
            ],
        }
        body = {"month": "2026-02", "company": "Globex", "position": "GX Dev", "gross": 10, "deductions": {"Tax": 2}}
        r = client.put("/api/work/months", json=body)
        assert r.status_code == 200 and r.json()["created"] is True
        assert client.get("/api/work/months").json()[-1] == {
            "month": "2026-02", "company": "Globex", "position": "GX Dev",
            "gross": 10.0, "deductions": {"Tax": 2.0}, "net": 8.0, "payment_date": None,
        }
        r = client.put("/api/work/months", json={"month": "2026-04"})
        assert r.status_code == 422 and "cannot be skipped" in r.json()["detail"]


def test_work_file_is_created_when_adding(settings, tmp_path):
    settings = replace(settings, work_path=tmp_path / "w" / "work.csv")
    r = TestClient(create_app(settings)).get("/work")
    assert r.status_code == 200 and "No companies yet" in r.text and "No months yet." in r.text
    store = LedgerStore(settings)
    store.save_month("2025-01", "Acme", "Dev", "10", {"Food": "2"}, "2025-02-01")
    assert settings.work_path.read_text(encoding="utf-8") == (
        "Month;Company;Position;Work - Salary Main;Received payment date;Food\n"
        "2025-01;Acme;Dev;10.00;2025-02-01;2.00\n"
    )


def test_salary_charts(settings, tmp_path):
    path = tmp_path / "work.csv"
    path.write_text(WORK_CSV, encoding="utf-8")
    r = TestClient(create_app(replace(settings, work_path=path))).get("/work")
    for title in ("Gross salary", "Net salary"):
        assert f"<h2>{title} <small>per month, 0 without a job</small></h2>" in r.text
    points = [json.loads(html.unescape(p)) for p in re.findall(r"data-points='([^']*)'", r.text)]
    gross, net = points
    assert [(p["label"], p["value_text"]) for p in net] == [
        ("2025-10", "$800.00"), ("2025-11", "$1,200.00"), ("2025-12", "$0.00"), ("2026-01", "$1,600.00"),
    ]
    assert gross[2]["change_text"] == "No job" and gross[0]["change_text"] == "Acme · Acme Jr"
    assert gross[2]["y"] == 100  # the no-job month is at 0, on the axis
    # Each salary in its own color: the net one green
    assert re.findall(r'class="line-chart" [^>]*style="--line: var\((--series-\d)\)"', r.text) == ["--series-1", "--series-3"]
    # The real salaries, at the CPI of the end of each month: none before base_currency.csv
    assert net[0]["real_text"] == "-" and net[-1]["real_text"] == "$1,600.00"
    assert r.text.count("(prices of 2026-01-31)") == 2


def test_work_form_fills_in_from_the_last_month(settings, tmp_path):
    path = tmp_path / "work.csv"
    path.write_text(
        "Month;Company;Position;Work - Salary Main;Tax;Received payment date\n"
        "2025-12;Acme;Acme Sr;1500.50;300.50;2026-01-05\n"
        "2026-01;Globex;GX Dev;2000.00;400.00;2026-02-05\n",
        encoding="utf-8",
    )
    r = TestClient(create_app(replace(settings, work_path=path))).get("/work")
    form = r.text.split('id="work-form"')[1].split("</form>")[0]
    (filled,) = re.findall(r"data-autofill='([^']*)'", form)
    assert json.loads(html.unescape(filled)) == {
        "company": "Globex", "position": "GX Dev", "gross": "2,000.00", "deduction:Tax": "400.00",
    }  # not the month nor the received payment date
    assert "Fill in from 2026-01" in form and 'value="2026-02"' in form
    # Not when editing a month: it already has its values
    form = TestClient(create_app(replace(settings, work_path=path))).get("/work/form", params={"month": "2025-12"}).text
    assert "data-autofill" not in form


def test_no_salary_charts_without_months(client):
    assert "Gross salary <small>" not in client.get("/work").text


class TestSalaries:
    """The salaries of the work history are generated each week, never stored in the ledger CSV."""

    @pytest.fixture
    def settings(self, settings, tmp_path):
        creditors = tmp_path / "creditors.yaml"
        creditors.write_text("De:\n  companies:\n    Acme: -5\n", encoding="utf-8")
        work = tmp_path / "work.csv"
        work.write_text(WORK_CSV, encoding="utf-8")
        return replace(with_salaries(settings, debit="De"), creditors_path=creditors, work_path=work)

    def salaries(self, settings) -> list[tuple]:
        rows = LedgerStore(settings).load().salary_rows
        return list(rows[["move_date", "base_movement", "acc_debit", "amount", "acc_credit", "comment"]].itertuples(index=False))

    def test_from_the_month_after_the_previous_month(self, settings):
        # Its initial week is the last one of 2025-12, from Monday 2025-12-22: the salaries
        # start on the first week of 2026-01, that of Thursday 2026-01-01, on Monday 2025-12-29
        path = settings.accounts_path
        path.write_text("previous_month: 2025-12\n" + path.read_text())
        assert [r[0] for r in self.salaries(settings)][::2] == [
            "2025-12-29", "2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26",
        ]

    def test_each_week_of_a_month_from_the_start_of_the_ledger(self, settings):
        # 2026-01 has 5 weeks, from Monday 2025-12-29 (Thursday 2026-01-01) on, but the
        # ledger starts on 2026-01-01: the months of Acme, and that week, are left out.
        # Each salary is followed by its deductions, split the same way, out of its account.
        assert self.salaries(settings) == [
            row
            for i, day in enumerate(["2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26"], 2)
            for row in (
                (day, "Work - Salary Main", "De", "400.00", "Eq", f"Globex (GX Dev): week {i}/5 of 2026-01"),
                (day, "Tax", "Eq", "80.00", "De", f"Globex (GX Dev): Tax, week {i}/5 of 2026-01"),
            )
        ]
        LedgerStore(settings).save_month("2026-02", "Globex", "GX Dev", "1000.01", {"Tax": "200.01"})
        assert [(r[0], r[1], r[3]) for r in self.salaries(settings)[8:]] == [
            row
            for day, salary, tax in (
                ("2026-02-02", "250.01", "50.01"), ("2026-02-09", "250.00", "50.00"),
                ("2026-02-16", "250.00", "50.00"), ("2026-02-23", "250.00", "50.00"),
            )
            for row in ((day, "Work - Salary Main", salary), (day, "Tax", tax))
        ]

    def test_are_in_the_ledger_and_owed_by_the_company(self, settings):
        snapshot = LedgerStore(settings).load()
        # The net salary: 1,600 of salary minus 320 of tax, with the initial -5 of Acme
        assert snapshot.ledger().balances()["De"] == -1285
        cc = snapshot.debt_accounts()[0]
        assert (cc.creditors["Globex"].amount, cc.creditors["Acme"].amount, cc.unassigned) == (-1280, -5, 0)
        assert snapshot.debt_accounts("2026-01")[0].creditors["Globex"].amount == -1280
        # Each salary's id is its month and week, and each deduction's also names it, so
        # editing other months or columns never changes them
        tax = deduction_id("Tax")
        assert [snapshot.ids[ix] for ix in snapshot.salary_rows.index] == [
            move_id for i in range(2, 6) for move_id in (f"W-2026-01-{i}", f"W-2026-01-{i}-{tax}")
        ]
        r = TestClient(create_app(settings)).get("/")
        # Each links to the row of its month in the Work tab
        assert r.text.count('href="/work#month-2026-01" title="Generated each week from the salary of the Work tab" aria-label="Generated each week from the salary of the Work tab">🟠</a>') == 8
        assert '<tr id="month-2026-01">' in TestClient(create_app(settings)).get("/work").text
        assert r.text.count('aria-label="Delete movement"') == 4  # only the CSV rows

    def test_cannot_be_stored_or_added_by_hand(self, settings):
        store = LedgerStore(settings)
        with pytest.raises(ValueError, match="'Work - Salary Main' are generated by pledger"):
            store.add("2026-01-05", "Work - Salary Main", "De", "Eq", "1", "Globex")
        assert "Work - Salary Main" not in TestClient(create_app(settings)).get("/form/bases", params={"move_type": "gain"}).text
        with settings.ledger_path.open("a") as f:
            f.write("2026-01-05;Work - Salary Main;De;1;Eq;Globex\n")
        with pytest.raises(LedgerDataError, match=re.escape("cannot have movements of ['Work - Salary Main']")):
            store.load()

    @pytest.mark.parametrize(
        "salary",
        ["        Work - Salary Main:\n          description: Main salary\n          debit: De\n",
         "          debit: De\n"],  # no fixed debit account
    )
    def test_their_base_movement_is_required(self, settings, salary):
        path = settings.base_movements_path
        path.write_text(path.read_text().replace(salary, "", 1))
        with pytest.raises(LedgerDataError, match=re.escape("'Work - Salary Main' is required for the salaries")):
            LedgerStore(settings).load()
        # Not without salaries to generate
        settings.work_path.write_text(WORK_CSV.rsplit("\n", 1)[0], encoding="utf-8")
        LedgerStore(settings).load()

    @pytest.mark.parametrize(
        ("column", "error"),
        [
            ("Taxes", "The column 'Taxes' of the work CSV is not a base movement"),
            ("Gift", "The column 'Gift' of the work CSV must be a deduction of the salary"),  # a gain
            ("Card fee", "The column 'Card fee' of the work CSV must be a deduction"),  # credits Cc, then Ca
            ("Initial (L)", "The column 'Initial \\(L\\)' of the work CSV must be a deduction"),  # internal
        ],
    )
    def test_deductions_must_be_losses_out_of_the_salary(self, settings, column, error):
        settings.work_path.write_text(WORK_CSV.replace(";Tax\n", f";{column}\n", 1), encoding="utf-8")
        if column == "Card fee":  # fine while it credits the account of the salary, but not another one
            path = settings.base_movements_path
            path.write_text(path.read_text().replace("          credit: Cc\n", "          credit: De\n", 1))
            LedgerStore(settings).load()
            path.write_text(path.read_text().replace("          debit: De\n", "          debit: Ca\n", 1))
        with pytest.raises(LedgerDataError, match=error):
            LedgerStore(settings).load()

    def test_a_new_deduction_adds_its_column(self, settings):
        store = LedgerStore(settings)
        store.save_month("2026-02", "Globex", "GX Dev", "1000", {"Tax": "200", "Food": "100"})
        assert settings.work_path.read_text(encoding="utf-8") == (
            "Month;Company;Position;Work - Salary Main;Tax;Food\n"
            "2025-10;Acme;Acme Jr;1000.00;200.00;0.00\n"
            "2025-11;Acme;Acme Sr;1500.50;300.50;0.00\n"
            "2025-12;;;;;\n"
            "2026-01;Globex;GX Dev;2000.00;400.00;0.00\n"
            "2026-02;Globex;GX Dev;1000.00;200.00;100.00"
        )
        assert store.load().work.months[-1].net == 700
        # Not of a base movement that can't be a deduction
        before = settings.work_path.read_text(encoding="utf-8")
        with pytest.raises(ValueError, match="Could not save 2026-02: .* 'Gift' of the work CSV must be a deduction"):
            store.save_month("2026-02", "Globex", "GX Dev", "1000", {"Gift": "1"})
        assert settings.work_path.read_text(encoding="utf-8") == before

    def test_work_tab_from_gross_to_net(self, settings):
        today = dt.date.today()
        settings.work_path.write_text(
            f"Month;Company;Position;Work - Salary Main;Tax;Food\n{today:%Y-%m};Globex;GX Dev;2000.00;400.00;0.00\n",
            encoding="utf-8",
        )
        r = TestClient(create_app(settings)).get("/work")
        assert f"<h2>From gross to net <small>{today.year}</small></h2>" in r.text
        table = r.text.split("<h2>From gross to net")[1].split("</table>")[0]
        assert "<td>Tax</td>" in table and "20.0%" in table and "<td>Food</td>" not in table  # none this year
        # From the gross salary on top down to the net one, not sortable
        assert table.index("<th>Gross salary</th>") < table.index("<td>Tax</td>") < table.index("<th>Net salary</th>")
        assert '<th class="num money">$2,000.00</th>' in table and '<th class="num money">$1,600.00</th>' in table
        assert "data-sortable" not in table
        assert 'name="deduction:Tax"' in r.text
