import datetime as dt
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from pledger.web.app import create_app
from pledger.web.recurring import build_due, load_recurring
from pledger.web.store import LedgerStore

RECURRING_YAML = """\
Rent:
  base_movement: Food
  credit: Bk
  amount: 300
  day: 10
  start: 2026-01
  comment: Flat
Allowance:
  base_movement: Withdraw
  weekday: monday
  start: 2026-02
"""


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr("pledger.web.store.today", lambda: dt.date(2026, 2, 15))  # a Sunday


@pytest.fixture
def recurring(settings, tmp_path):
    path = tmp_path / "recurring.yaml"
    path.write_text(RECURRING_YAML, encoding="utf-8")
    return replace(settings, recurring_path=path)


def due(settings) -> list[tuple[str, str, int]]:
    data = build_due(settings.recurring_path, LedgerStore(settings).load())
    assert data["due_error"] is None
    return [(d["name"], d["date"], d["in_days"]) for d in data["due"]]


class TestDue:
    def test_dates(self, recurring):
        names_dates = [(n, d) for n, d, _ in due(recurring)]
        # January's rent, every Monday of February, February's rent, and next Monday (upcoming)
        assert names_dates == [
            ("Rent", "2026-01-10"), ("Allowance", "2026-02-02"), ("Allowance", "2026-02-09"),
            ("Rent", "2026-02-10"), ("Allowance", "2026-02-16"),
        ]
        assert [d for _, _, d in due(recurring)] == [-36, -13, -6, -5, 1]

    def test_a_movement_in_its_period_settles_it(self, recurring):
        store = LedgerStore(recurring)
        store.add("2026-01-31", "Food", "Eq", "Bk", "310")  # another amount and day: same month
        store.add("2026-02-08", "Withdraw", "Ca", "Bk", "5")  # the Sunday of the week of the 2nd
        store.add("2026-02-10", "Food", "Eq", "Ca", "300")  # another account: not the rent
        assert [(n, d) for n, d, _ in due(recurring)] == [
            ("Allowance", "2026-02-09"), ("Rent", "2026-02-10"), ("Allowance", "2026-02-16"),
        ]

    def test_values_of_the_form(self, recurring):
        rent, allowance = (
            next(d for d in build_due(recurring.recurring_path, LedgerStore(recurring).load())["due"] if d["name"] == n)
            for n in ("Rent", "Allowance")
        )
        assert rent["values"] == {
            "move_date": "2026-01-10", "base_movement": "Food", "acc_debit": "Eq", "acc_credit": "Bk",
            "amount": "300.00", "comment": "Flat",
        }
        assert rent["every"] == "month" and allowance["every"] == "week"
        assert allowance["values"]["amount"] == ""  # filled in when adding it
        assert (allowance["values"]["acc_debit"], allowance["values"]["acc_credit"]) == ("Ca", "Bk")  # fixed

    def test_last_day_of_shorter_months_start_and_end(self, settings, tmp_path):
        path = tmp_path / "recurring.yaml"
        path.write_text("Fee:\n  base_movement: Food\n  credit: Ca\n  day: 31\n  start: 2025-11\n  end: 2025-12\n")
        assert [(n, d) for n, d, _ in due(replace(settings, recurring_path=path))] == [
            ("Fee", "2025-11-30"), ("Fee", "2025-12-31"),
        ]
        path.write_text("Fee:\n  base_movement: Food\n  credit: Ca\n  day: 1\n")  # from this month
        assert [(n, d) for n, d, _ in due(replace(settings, recurring_path=path))] == [("Fee", "2026-02-01")]

    def test_no_file(self, settings):
        assert build_due(None, LedgerStore(settings).load()) == {"due": [], "due_error": None}
        assert due(replace(settings, recurring_path=settings.ledger_path.parent / "missing.yaml")) == []


class TestInvalid:
    @pytest.mark.parametrize(("yaml_text", "error"), [
        ("- a list", "must map each recurring movement"),
        ("X: 3", "must have its fields"),
        ("X:\n  base_movement: Nope\n  day: 1", "needs the base_movement"),
        ("X:\n  base_movement: Food\n  day: 1", "needs a credit account that 'Food' can take, not None"),
        ("X:\n  base_movement: Withdraw\n  debit: Bk\n  day: 1", "always has 'Ca' as its debit account"),
        ("X:\n  base_movement: Withdraw", "either a day of the month or a weekday"),
        ("X:\n  base_movement: Withdraw\n  day: 1\n  weekday: monday", "either a day"),
        ("X:\n  base_movement: Withdraw\n  day: 32", "from 1 to 31"),
        ("X:\n  base_movement: Withdraw\n  weekday: funday", "a weekday like 'monday'"),
        ("X:\n  base_movement: Withdraw\n  day: 1\n  start: 2026-13", "must be a month like 2026-01"),
        ("X:\n  base_movement: Withdraw\n  day: 1\n  start: 2026-03\n  end: 2026-02", "ends before it starts"),
        ("X:\n  base_movement: Withdraw\n  day: 1\n  amount: -3", "cannot be negative"),
        ("X:\n  base_movement: Withdraw\n  day: 1\n  every: week", "unknown fields: ['every']"),
        ("X: [", "while parsing"),
    ])
    def test_errors(self, settings, tmp_path, yaml_text, error):
        path = tmp_path / "recurring.yaml"
        path.write_text(yaml_text)
        data = build_due(path, LedgerStore(settings).load())
        assert data["due"] == [] and error in data["due_error"]

    def test_raises_when_loaded_directly(self, settings, tmp_path):
        path = tmp_path / "recurring.yaml"
        path.write_text("X:\n  base_movement: Withdraw")
        with pytest.raises(ValueError, match="Recurring movement 'X'"):
            load_recurring(path, LedgerStore(settings).load())


class TestPage:
    def test_due_panel(self, recurring):
        client = TestClient(create_app(recurring))
        r = client.get("/")
        panel = r.text.split('<div class="panel due">')[1].split("</table>")[0]
        assert "<h2>Due <small>5</small></h2>" in panel
        assert "36 days late" in panel and "in 1 day" in panel and "every week" in panel
        assert panel.count(">Add</button>") == 2  # the rents; the allowances have no amount
        assert panel.count(">Fill in</button>") == 5

    def test_no_panel_without_recurring(self, client):
        assert 'class="panel due"' not in client.get("/").text

    def test_error_in_the_panel(self, settings, tmp_path):
        path = tmp_path / "recurring.yaml"
        path.write_text("X: 3")
        r = TestClient(create_app(replace(settings, recurring_path=path))).get("/")
        assert r.status_code == 200 and "Recurring movements: Recurring movement" in r.text

    def test_add_settles_it(self, recurring):
        client = TestClient(create_app(recurring))
        rent = client.get("/api/recurring/due").json()["due"][0]
        r = client.post("/movements", data=rent["values"])
        assert "Added Food" in r.text and r.headers["HX-Trigger"] == "ledger-changed"
        assert [d["date"] for d in client.get("/api/recurring/due").json()["due"] if d["name"] == "Rent"] == ["2026-02-10"]
        partial = client.get("/", headers={"HX-Request": "true"}).text
        assert "<h2>Due <small>4</small></h2>" in partial

    def test_fill_in_the_form(self, recurring):
        client = TestClient(create_app(recurring))
        values = client.get("/api/recurring/due").json()["due"][0]["values"]
        form = client.get("/form/movement", params=values).text
        assert 'value="2026-01-10"' in form and 'value="300.00"' in form and 'value="Flat"' in form
        assert "<option selected>Bk</option>" in form
