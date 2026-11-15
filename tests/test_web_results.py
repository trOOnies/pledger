"""Tests for the Results tab."""

import datetime as dt

import pytest
from helpers import HTMX

from pledger.web.store import (
    LedgerStore,
)
from pledger.web.views import (
    AVERAGE_MONTH_DAYS,
    build_results,
    equity_metrics,
    expense_months,
)
from pledger.web.views.charts import stacked_column_chart


class TestResultProfits:
    def test_profit_by_result_type(self, settings):
        profits = build_results(LedgerStore(settings).load())["profits"]
        (op, gifts) = profits
        assert op["name"] == "Operative" and op["value"] == pytest.approx(1000.5 - 50.25) and op["share"] is None
        assert {c["name"]: c["value"] for c in op["children"]} == {
            "Salary": pytest.approx(1000.5), "Food": pytest.approx(-50.25), "Card fee": 0.0,
        }
        assert gifts["value"] == pytest.approx(10)
        # On the largest absolute value: the salary
        assert op["children"][0]["width"] == pytest.approx(100) and op["children"][1]["width"] == pytest.approx(50.25 / 10.005)

    def test_third_column(self, client):
        panel = client.get("/results").text.split("Gains &amp; losses by result type")[1].split("</section>")[0]
        assert "<h3>Gains " in panel and "<h3>Losses " in panel and "<h3>Profit " in panel
        assert '<span class="bar loss"' in panel.split("<h3>Profit ")[1]  # the food, a negative profit


class TestExpenseMonths:
    """Equity in months of the expenses of the loss result types, at each month end."""

    @staticmethod
    def span(snapshot, end):
        """The months of the averages up to `end`: from the first date of the ledger (within the year)."""
        first = dt.date.fromisoformat(snapshot.all_rows.move_date.min())
        return ((dt.date.fromisoformat(end) - first).days + 1) / AVERAGE_MONTH_DAYS

    def test_the_loss_result_types_but_the_internal_ones_by_default(self, settings):
        snapshot = LedgerStore(settings).load()
        assert [(f["name"], f["checked"]) for f in expense_months(snapshot)["filters"]] == [("Op", True)]

    def test_equity_over_the_average_monthly_losses(self, settings):
        snapshot = LedgerStore(settings).load()
        (row,) = expense_months(snapshot, "2026-01")["months"]
        assert row["end"] == "2026-01-31"
        assert row["equity"] == pytest.approx(100.5 + 1000.5 - 50.25 + 10)  # with the initial amounts
        assert row["averages"] == {"Op": pytest.approx(50.25 / self.span(snapshot, "2026-01-31"))}
        assert row["months"] == pytest.approx(row["equity"] / row["monthly"])

    def test_every_month_of_the_ledger_by_default(self, settings):
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write("2026-03-05;Food;Eq;10;Ca;\n")
        snapshot = LedgerStore(settings).load()
        expenses = expense_months(snapshot)
        months = [r["month"] for r in expenses["months"]]
        assert months[-3:] == ["2026-01", "2026-02", "2026-03"]  # February too, without movements
        assert len(expenses["chart"]["columns"]) == len(expenses["raw_chart"]["columns"]) == len(months)
        march = expenses["months"][-1]
        assert march["averages"]["Op"] == pytest.approx(60.25 / self.span(snapshot, "2026-03-31"))

    def test_without_result_types_there_are_no_months(self, settings):
        expenses = expense_months(LedgerStore(settings).load(), "2026-01", selected=[])
        assert [f["checked"] for f in expenses["filters"]] == [False]
        assert expenses["months"][0]["months"] is None and expenses["chart"] is None and expenses["raw_chart"] is None

    def test_the_panel(self, client):
        panel = client.get("/results").text.split('id="expenses-panel"')[1].split("</section>")[0]
        assert 'name="expenses-raw"' in panel and 'name="expense_types" value="Op" checked' in panel
        assert 'data-raw="false"' in panel and 'class="segment"' in panel
        # Its filter re-renders it alone, even with no result type ticked
        r = client.get("/results/expenses", params={"month": "2026-01", "expenses_filtered": "true"})
        assert r.text.count("No expenses of the selected result types") == 2 and 'value="Op" >' in r.text
        r = client.get("/results", params={"expenses_filtered": "true", "expense_types": "Op"}, headers=HTMX)
        assert 'value="Op" checked' in r.text


WORK_YAML = (
    "  Work:\n    after: Gifts\n    long_name: Work\n    description: Relying on having a job\n    employment: true\n"
    "    base_movements:\n      loss:\n        Union fee: Union fee\n"
)


def with_work(settings) -> None:
    """A Work result type, tagged employment, with a Union fee loss of 20 on 2026-01-04."""
    path = settings.base_movements_path
    path.write_text(path.read_text().replace("transfer:\n  Withdraw:", WORK_YAML + "transfer:\n  Withdraw:", 1))
    with settings.ledger_path.open("a", encoding="utf-8") as f:
        f.write("2026-01-04;Union fee;Eq;20;Ca;\n")


def with_initial_loss(settings) -> None:
    """Owe 30 on the credit card from the start: an Initial (L) loss of 2026-01-01."""
    path = settings.accounts_path
    old = "  Cc:\n    description: Credit card\n    initial_amount: 0"
    path.write_text(path.read_text().replace(old, old.replace(": 0", ": 30")))


class TestExpenses:
    """The expenses whose months equity covers: the card of the Results tab and the panel, alike."""

    FOOD_MONTHLY = 50.25 / (5 / AVERAGE_MONTH_DAYS)  # from 2025-12-31 to 2026-01-04, the last date
    FOOD_JANUARY = 50.25 / (32 / AVERAGE_MONTH_DAYS)  # from 2025-12-31 to 2026-01-31

    @staticmethod
    def both(settings) -> tuple:
        snapshot = LedgerStore(settings).load()
        return equity_metrics(snapshot)["monthly_expenses"], expense_months(snapshot, "2026-01")["months"][0]

    def test_employment_is_a_tag_of_the_result_types(self, settings):
        with_work(settings)
        result_types = LedgerStore(settings).load().result_types.values()
        assert [rt.name for rt in result_types if rt.employment] == ["Work"]

    def test_the_losses_of_employment_result_types_are_left_out(self, settings):
        with_work(settings)
        monthly, row = self.both(settings)
        assert monthly == pytest.approx(self.FOOD_MONTHLY)  # not the union fee
        assert row["averages"] == {"Op": pytest.approx(self.FOOD_JANUARY)}
        # Not even offered in the filter of the panel
        assert [f["name"] for f in expense_months(LedgerStore(settings).load())["filters"]] == ["Op"]

    def test_initial_losses_are_never_expenses(self, settings):
        with_initial_loss(settings)
        snapshot = LedgerStore(settings).load()
        assert "Initial (L)" in {m.move_base.name for m in snapshot.ledger().movements}
        monthly, row = self.both(settings)
        assert monthly == pytest.approx(self.FOOD_MONTHLY)
        assert row["averages"] == {"Op": pytest.approx(self.FOOD_JANUARY)}
        # Not even with the initial movements of the Results tab: Init is never a result type of expenses
        assert [f["name"] for f in build_results(snapshot, initial=True)["expenses"]["filters"]] == ["Op"]

    def test_the_card_matches_the_last_month_of_the_panel(self, settings):
        with_initial_loss(settings)
        with_work(settings)
        snapshot = LedgerStore(settings).load()
        last = expense_months(snapshot)["months"][-1]
        metrics = equity_metrics(snapshot, last["month"])  # at the same end, the end of January
        assert metrics["monthly_expenses"] == pytest.approx(last["monthly"]) == pytest.approx(self.FOOD_JANUARY)
        assert metrics["months_covered"] == pytest.approx(last["months"])

    def test_the_badge_and_the_form(self, client, settings):
        with_work(settings)
        page = client.get("/results").text
        row = page.split("<strong>Work</strong>")[1].split("</tr>")[0]
        assert '<span class="badge employment"' in row and ">Employment</span>" in row
        assert '<span class="badge employment"' not in page.split("<strong>Op</strong>")[1].split("</tr>")[0]
        r = client.post("/results/result-types", data={"name": "W Taxes", "long_name": "Work Taxes", "description": "d", "employment": "true"})
        assert "Added result type W Taxes" in r.text
        assert "  W Taxes:\n    after: Work\n    long_name: Work Taxes\n    description: d\n    employment: true\n" in settings.base_movements_path.read_text()
        assert next(rt for rt in LedgerStore(settings).load().result_types.values() if rt.name == "W Taxes").employment
        api = {rt["name"]: rt["employment"] for rt in client.get("/api/result-types").json()}
        assert api == {"Op": False, "Gifts": False, "Work": True, "W Taxes": True, "Init": False}


def test_stacked_column_chart():
    chart = stacked_column_chart(
        [{"month": "2026-01", "values": {"A": 30.0, "B": 10.0}}, {"month": "2026-02", "values": {"A": 20.0}}],
        {"A": 1, "B": 2},
    )
    first, second = chart["columns"]
    assert first["total"] == 40 and second["total"] == 20
    # From the top down: B on top of A, each a share of the column
    assert [(s["name"], s["slot"], s["height"]) for s in first["segments"]] == [("B", 2, 25.0), ("A", 1, 75.0)]
    assert second["height"] == pytest.approx(first["height"] / 2)
    assert stacked_column_chart([{"month": "2026-01", "values": {"A": 0.0}}], {"A": 1}) is None
