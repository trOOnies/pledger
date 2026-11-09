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
        # The initial one only with the initial movements, and not ticked: it is internal
        filters = expense_months(snapshot, initial=True)["filters"]
        assert [(f["name"], f["checked"]) for f in filters] == [("Op", True), ("Init", False)]

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
