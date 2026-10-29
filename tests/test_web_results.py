"""Tests for the Results tab."""

import pytest

from pledger.web.store import (
    LedgerStore,
)
from pledger.web.views import (
    build_results,
)


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
