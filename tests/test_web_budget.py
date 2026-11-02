"""Tests for the Budget tab: the monthly budgets and the saving goals."""

import datetime as dt
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import HTMX

from pledger.web.app import create_app
from pledger.web.store import LedgerDataError, LedgerStore
from pledger.web.views import build_budgets, build_goals


@pytest.fixture
def settings(settings, tmp_path):
    return replace(settings, budgets_path=tmp_path / "budgets.csv", goals_path=tmp_path / "goals.csv")


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


@pytest.fixture
def store(settings):
    return LedgerStore(settings)


def spend(store, month, amount, comment=""):
    store.add(f"{month}-05", "Food", "Eq", "Ca", amount, comment)


def budgets(store, month):
    return build_budgets(store.load(), store.load_budgets(), month)


def test_new_plan_files_dont_stop_pledger_from_starting(settings):
    assert not {settings.budgets_path, settings.goals_path} & set(settings.expected_files())


class TestBudgets:
    def test_spent_against_the_budget(self, store):
        spend(store, "2026-03", "30")
        spend(store, "2026-03", "20")
        spend(store, "2026-04", "999")  # another month
        store.set_budget("2026-03", "Food", "40")
        view = budgets(store, "2026-03")
        row = view["budgets"][0]
        assert (row["base"], row["budget"], row["spent"], row["left"]) == ("Food", 40, 50, -10)
        assert row["over"] and row["share"] == 125 and view["over"] == 1
        assert (view["budgeted"], view["spent"], view["left"]) == (40, 50, -10)

    def test_holds_until_changed_and_past_months_keep_theirs(self, store):
        store.set_budget("2026-03", "Food", "40")
        store.set_budget("2026-05", "Food", "60")
        amounts = {m: [b["budget"] for b in budgets(store, m)["budgets"]] for m in ("2026-02", "2026-04", "2026-05", "2026-06")}
        assert amounts == {"2026-02": [], "2026-04": [40], "2026-05": [60], "2026-06": [60]}

    def test_same_month_replaces(self, store):
        store.set_budget("2026-03", "Food", "40")
        store.set_budget("2026-03", "Food", "50")
        assert len(store.load_budgets()) == 1 and budgets(store, "2026-03")["budgets"][0]["budget"] == 50

    def test_stop_keeps_the_months_before(self, store):
        store.set_budget("2026-03", "Food", "40")
        store.set_budget("2026-06", "Food", "70")
        store.stop_budget("2026-05", "Food")
        assert [b["budget"] for b in budgets(store, "2026-04")["budgets"]] == [40]
        assert budgets(store, "2026-05")["budgets"] == [] and budgets(store, "2026-07")["budgets"] == []

    def test_stop_with_no_earlier_budget_leaves_nothing(self, store):
        store.set_budget("2026-03", "Food", "40")
        store.stop_budget("2026-03", "Food")
        assert len(store.load_budgets()) == 0

    def test_not_budgeted(self, store):
        spend(store, "2026-03", "30")
        assert budgets(store, "2026-03")["not_budgeted"] == 30

    @pytest.mark.parametrize(("month", "base", "amount", "error"), [
        ("2026-13", "Food", "1", "Invalid month"),
        ("2026-03", "Salary", "1", "Pick a loss base movement"),
        ("2026-03", "", "1", "Pick a loss base movement"),
        ("2026-03", "Food", "", "amount is required"),
        ("2026-03", "Food", "-1", "cannot be negative"),
        ("2026-03", "Food", "x", "Invalid amount"),
    ])
    def test_invalid(self, store, month, base, amount, error):
        with pytest.raises(ValueError, match=error):
            store.set_budget(month, base, amount)

    def test_page(self, store, client):
        spend(store, "2026-03", "30", "a note")
        assert client.get("/budget").status_code == 200
        assert f'value="{dt.date.today().isoformat()[:7]}"' in client.get("/budget").text
        r = client.post("/budget/budgets", data={"month": "2026-03", "base_movement": "Food", "amount": "40"}, headers=HTMX)
        assert "Budgeted Food from 2026-03 on" in r.text and "<!doctype" not in r.text and "$30.00" in r.text
        r = client.post("/budget/budgets", data={"month": "2026-03", "base_movement": "", "amount": "7.5"}, headers=HTMX)
        assert 'class="msg error"' in r.text and 'value="7.5"' in r.text
        r = client.post("/budget/budgets/stop", data={"month": "2026-03", "base_movement": "Food"}, headers=HTMX)
        assert "Stopped budgeting Food" in r.text and "No budgets in 2026-03" in r.text
        assert '<a href="/budget"' in client.get("/").text

    def test_never_touches_the_ledger(self, store, client, settings):
        before = settings.ledger_path.read_bytes()
        client.post("/budget/budgets", data={"month": "2026-03", "base_movement": "Food", "amount": "40"})
        assert settings.ledger_path.read_bytes() == before

    def test_invalid_csv(self, store, settings):
        settings.budgets_path.write_text("month;amount\n2026-03;1\n")
        with pytest.raises(LedgerDataError):
            store.load_budgets()


class TestGoals:
    def goals(self, store):
        return build_goals(store.load(), store.load_goals())["goals"]

    def test_saved_is_the_balance_of_the_account(self, store):
        balance = store.load().balances_today()["Ca"]
        store.save_goal("Car", "Ca", balance * 2)
        goal = self.goals(store)[0]
        assert goal["saved"] == pytest.approx(balance) and goal["left"] == pytest.approx(balance)
        assert goal["share"] == pytest.approx(50) and not goal["done"] and goal["monthly"] is None

    def test_reached(self, store):
        store.save_goal("Small", "Ca", "0.01")
        goal = self.goals(store)[0]
        assert goal["done"] and goal["left"] == 0

    def test_monthly_up_to_the_due_date(self, store):
        balance = store.load().balances_today()["Ca"]
        today = store.load().today
        due = (today.replace(day=1) + dt.timedelta(days=100)).replace(day=1)  # three calendar months away
        store.save_goal("Car", "Ca", balance + 300, due.isoformat())
        goal = self.goals(store)[0]
        assert goal["monthly"] == pytest.approx(100) and not goal["overdue"]

    def test_overdue(self, store):
        store.save_goal("Car", "Ca", "99999999", "2020-01-01")
        goal = self.goals(store)[0]
        assert goal["overdue"] and goal["monthly"] is None

    def test_shared_account_is_flagged(self, store):
        store.save_goal("A", "Ca", "1")
        assert not self.goals(store)[0]["shared"]
        store.save_goal("B", "Ca", "1")
        assert all(g["shared"] for g in self.goals(store))

    def test_same_name_updates(self, store):
        store.save_goal("Car", "Ca", "1")
        store.save_goal(" Car ", "Ca", "2")
        assert [g["target"] for g in self.goals(store)] == [2]

    def test_a_failed_update_keeps_the_goal(self, store):
        store.save_goal("Car", "Ca", "1")
        with pytest.raises(ValueError, match="asset account"):
            store.save_goal("Car", "Nope", "2")
        assert [g["target"] for g in self.goals(store)] == [1]

    @pytest.mark.parametrize(("name", "account", "target", "due", "error"), [
        ("", "Ca", "1", "", "name is required"),
        ("Car", "Eq", "1", "", "asset account"),
        ("Car", "Ca", "", "", "target is required"),
        ("Car", "Ca", "0", "", "more than 0"),
        ("Car", "Ca", "1", "soon", "Invalid due date"),
    ])
    def test_invalid(self, store, name, account, target, due, error):
        with pytest.raises(ValueError, match=error):
            store.save_goal(name, account, target, due)

    def test_page(self, client):
        r = client.post("/budget/goals", data={"month": "2026-03", "name": "Car", "account": "Ca", "target": "10"}, headers=HTMX)
        assert "Saved the goal Car" in r.text and "<strong>Car</strong>" in r.text
        r = client.post("/budget/goals", data={"month": "2026-03", "name": "", "account": "Ca", "target": "10"}, headers=HTMX)
        assert 'class="msg error"' in r.text
        r = client.post("/budget/goals/delete", data={"name": "Car", "month": "2026-03"}, headers=HTMX)
        assert "No saving goals yet" in r.text
