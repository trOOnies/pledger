"""Tests for the Results tab."""

import datetime as dt

import pytest
from helpers import HTMX

from pledger.web.store import (
    LedgerDataError,
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


EMPLOYMENT_FOOD = "        Food:\n          description: Groceries\n"


def tag_employment(settings, yaml_entry: str = EMPLOYMENT_FOOD) -> None:
    """Tag a base movement of the base movements YAML (Food by default) as employment."""
    path = settings.base_movements_path
    path.write_text(path.read_text().replace(yaml_entry, yaml_entry + "          employment: true\n", 1))


def with_initial_loss(settings) -> None:
    """Owe 30 on the credit card from the start: an Initial (L) loss of 2026-01-01."""
    path = settings.accounts_path
    old = "  Cc:\n    description: Credit card\n    initial_amount: 0"
    path.write_text(path.read_text().replace(old, old.replace(": 0", ": 30")))


class TestExpenses:
    """The expenses whose months equity covers: the card of the Results tab and the panel, alike."""

    @staticmethod
    def both(settings) -> tuple:
        snapshot = LedgerStore(settings).load()
        return equity_metrics(snapshot)["monthly_expenses"], expense_months(snapshot, "2026-01")["months"][0]

    def test_employment_is_a_tag_of_the_gains_and_losses(self, settings):
        tag_employment(settings)
        bases = LedgerStore(settings).load().base_movements
        assert bases["Food"].employment and not bases["Salary"].employment

    @pytest.mark.parametrize("entry, error", [
        ("        Food:\n          description: Groceries\n          employment: yes please\n", "must set 'employment' to true or false"),
        ("  Withdraw:\n    description: Bank to cash\n    employment: true\n", "can only be tagged employment if it is a non-internal gain or loss"),
    ])
    def test_bad_employment_tags(self, settings, entry, error):
        path = settings.base_movements_path
        old = entry.rsplit("\n", 2)[0] + "\n"  # without the tag
        path.write_text(path.read_text().replace(old, entry, 1))
        with pytest.raises(LedgerDataError, match=error):
            LedgerStore(settings).load()

    def test_employment_losses_are_left_out(self, settings):
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write("2026-01-04;Card fee;Eq;20;Cc;\n")
        tag_employment(settings)
        monthly, row = self.both(settings)
        span = 5 / AVERAGE_MONTH_DAYS  # 2025-12-31 to 2026-01-04
        assert monthly == pytest.approx(20 / span)  # the card fee, not the food
        assert row["averages"] == {"Op": pytest.approx(20 / (32 / AVERAGE_MONTH_DAYS))}  # from 2025-12-31 to 2026-01-31

    def test_only_employment_losses_are_no_expenses(self, settings):
        tag_employment(settings)
        monthly, row = self.both(settings)
        assert monthly is None and row["months"] is None
        # Op has no other losses than the card fee, so it is still listed
        assert [f["name"] for f in expense_months(LedgerStore(settings).load())["filters"]] == ["Op"]

    def test_initial_losses_are_never_expenses(self, settings):
        with_initial_loss(settings)
        snapshot = LedgerStore(settings).load()
        assert "Initial (L)" in {m.move_base.name for m in snapshot.ledger().movements}
        monthly, row = self.both(settings)
        assert monthly == pytest.approx(50.25 / (5 / AVERAGE_MONTH_DAYS))  # only the food
        assert row["averages"] == {"Op": pytest.approx(50.25 / (32 / AVERAGE_MONTH_DAYS))}
        # Not even with the initial movements of the Results tab: Init is never a result type of expenses
        assert [f["name"] for f in build_results(snapshot, initial=True)["expenses"]["filters"]] == ["Op"]

    def test_the_card_matches_the_last_month_of_the_panel(self, settings):
        with_initial_loss(settings)
        tag_employment(settings, "        Card fee:\n          description: Credit card fee\n          credit: Cc\n")
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write("2026-01-04;Card fee;Eq;20;Cc;\n")
        snapshot = LedgerStore(settings).load()
        last = expense_months(snapshot)["months"][-1]
        metrics = equity_metrics(snapshot, last["month"])  # at the same end, the end of January
        assert metrics["monthly_expenses"] == pytest.approx(last["monthly"]) == pytest.approx(50.25 / (32 / AVERAGE_MONTH_DAYS))
        assert metrics["months_covered"] == pytest.approx(last["months"])

    def test_the_badge_and_the_form(self, client, settings):
        tag_employment(settings)
        page = client.get("/results").text
        food = page.split("<strong>Food</strong>")[1].split("</td>")[0]
        assert '<span class="badge employment"' in food and ">Employment</span>" in food
        assert "<strong>Salary</strong></td>" in page
        r = client.post("/results/base-movements", data={
            "name": "Union", "move_type": "loss", "description": "Union fee", "result_type": "Op", "employment": "true",
        })
        assert "Added base movement Union" in r.text
        assert "        Union:\n          description: Union fee\n          employment: true\n" in settings.base_movements_path.read_text()
        assert LedgerStore(settings).load().base_movements["Union"].employment
        r = client.post("/results/base-movements", data={
            "name": "Lend", "move_type": "transfer", "description": "d", "debit": "Ca", "credit": "Bk", "employment": "true",
        })
        assert "Only gains and losses can be tagged employment" in r.text
        api = {b["name"]: b["employment"] for b in client.get("/api/base-movements").json()}
        assert api["Union"] and api["Food"] and not api["Salary"]


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
