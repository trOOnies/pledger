"""Tests for pledger.weeks."""

import datetime as dt
from itertools import pairwise

import pytest

from pledger.weeks import Week, prorate, prorate_by_week


def mondays(weeks: list[Week]) -> list[str]:
    return [w.start.isoformat() for w in weeks]


class TestWeek:
    def test_of_a_day(self):
        assert Week.of(dt.date(2026, 1, 1)) == Week(dt.date(2025, 12, 29))
        assert Week.of(dt.date(2026, 1, 4)).end == dt.date(2026, 1, 4)  # a Sunday
        assert Week.of(dt.date(2026, 1, 5)).start == dt.date(2026, 1, 5)

    def test_starts_on_a_monday(self):
        with pytest.raises(ValueError, match="starts on a Monday, not on Thursday 2026-01-01"):
            Week(dt.date(2026, 1, 1))

    @pytest.mark.parametrize(
        ("monday", "month"),
        [
            ("2025-12-29", "2026-01"),  # 3 days in December, 4 in January
            ("2026-01-26", "2026-01"),  # 6 days in January, 1 in February
            ("2026-02-23", "2026-02"),  # 6 days in February, 1 in March
            ("2026-03-30", "2026-04"),  # 2 days in March, 5 in April
            ("2026-06-29", "2026-07"),  # 2 days in June, 5 in July
        ],
    )
    def test_belongs_to_the_month_with_4_of_its_days(self, monday, month):
        assert Week(dt.date.fromisoformat(monday)).month == month

    def test_of_a_month(self):
        assert mondays(Week.of_month("2026-01")) == ["2025-12-29", "2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26"]
        assert mondays(Week.of_month("2026-02")) == ["2026-02-02", "2026-02-09", "2026-02-16", "2026-02-23"]
        assert mondays(Week.of_month("2026-03")) == ["2026-03-02", "2026-03-09", "2026-03-16", "2026-03-23"]

    def test_each_week_belongs_to_a_single_month(self):
        months = [f"{y}-{m:02d}" for y in (2025, 2026, 2027, 2028) for m in range(1, 13)]
        weeks = [w for m in months for w in Week.of_month(m)]
        assert all(b.start - a.start == dt.timedelta(weeks=1) for a, b in pairwise(weeks))
        assert {len(Week.of_month(m)) for m in months} == {4, 5}


class TestProrate:
    @pytest.mark.parametrize(
        ("total", "parts", "expected"),
        [
            (2000, 5, [400, 400, 400, 400, 400]),
            (1000.01, 4, [250.01, 250, 250, 250]),
            (10, 3, [3.34, 3.33, 3.33]),
            (0, 4, [0, 0, 0, 0]),
        ],
    )
    def test_equal_parts_to_the_cent(self, total, parts, expected):
        assert prorate(total, parts) == expected
        assert round(sum(prorate(total, parts)), 2) == round(total, 2)

    def test_needs_parts(self):
        with pytest.raises(ValueError, match="Cannot split into 0 parts"):
            prorate(1, 0)

    def test_by_week(self):
        assert [(w.start.isoformat(), v) for w, v in prorate_by_week("2026-02", 1000.01)] == [
            ("2026-02-02", 250.01), ("2026-02-09", 250), ("2026-02-16", 250), ("2026-02-23", 250),
        ]
