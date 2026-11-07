"""Tests for the reserved word `estimated` of the CSVs of values, and filling them in on startup."""

import datetime as dt
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from pledger.web.app import create_app
from pledger.web.store import (
    LedgerDataError,
    LedgerStore,
    service_cells,
    service_months,
)
from pledger.web.views import build_currencies


class Base:
    """Pesos and dollars, on 2026-03-11, with services of De."""

    today = dt.date(2026, 3, 11)

    @pytest.fixture
    def settings(self, settings, tmp_path, monkeypatch):
        monkeypatch.setattr("pledger.web.store.common.today", lambda: self.today)
        creditors = tmp_path / "creditors.yaml"
        creditors.write_text(
            "services:\n  Rent:\n    base_movement: Food\n  Gas:\n    base_movement: Food\n",
            encoding="utf-8",
        )
        services = tmp_path / "services.csv"
        services.write_text("month;Rent;Gas\n", encoding="utf-8")
        return replace(settings, creditors_path=creditors, services_path=services)

    @staticmethod
    def write(settings, inflation=None, values=None, services=None):
        folder = settings.currencies_path.parent
        if inflation is not None:
            (folder / "base_currency.csv").write_text(f"month;monthly_inflation\n{inflation}", encoding="utf-8")
        if values is not None:
            (folder / "currencies.csv").write_text(f"date;USD\n{values}", encoding="utf-8")
        if services is not None:
            settings.services_path.write_text(f"month;Rent;Gas\n{services}", encoding="utf-8")


class TestEstimates(Base):
    INFLATION = "2026-01;10.00%\n2026-02;estimated\n2026-03;estimated\n"

    def test_inflation_is_the_last_known_one(self, settings):
        self.write(settings, self.INFLATION)
        base = LedgerStore(settings).load().base_currency
        assert list(base.monthly_inflation) == pytest.approx([0.10, 0.10, 0.10])
        assert [str(m) for m in base.estimated] == ["2026-02", "2026-03"]

    def test_inflation_needs_a_known_one_before(self, settings):
        self.write(settings, "2026-01;estimated\n2026-02;1%\n")
        with pytest.raises(LedgerDataError, match="no known inflation before"):
            LedgerStore(settings).load()

    def test_services_grow_by_the_inflation_of_each_month(self, settings):
        self.write(settings, self.INFLATION, services="2026-01;100.00;50.00\n2026-02;estimated;\n2026-03;estimated;estimated\n")
        snapshot = LedgerStore(settings).load()
        assert dict(service_months(snapshot)) == {
            "2026-01": {"Rent": 100.0, "Gas": 50.0},
            "2026-02": {"Rent": 110.0},
            # Gas has no value in 2026-02: its last known one is two months of inflation old
            "2026-03": {"Rent": 121.0, "Gas": 60.5},
        }

    def test_a_month_after_the_inflation_takes_its_last_one(self, settings):
        self.write(settings, "2026-01;10.00%\n", services="2026-01;100.00;\n2026-02;estimated;\n")
        assert dict(service_months(LedgerStore(settings).load()))["2026-02"] == {"Rent": 110.0}

    def test_a_service_needs_a_known_value_before(self, settings):
        self.write(settings, self.INFLATION, services="2026-01;estimated;\n")
        with pytest.raises(LedgerDataError, match="Rent estimated in 2026-01, with no known value before it"):
            LedgerStore(settings).load()

    def test_estimated_services_are_owed(self, settings):
        self.write(settings, self.INFLATION, services="2026-01;100.00;\n2026-02;estimated;\n")
        rows = LedgerStore(settings).load().service_rows
        assert sum(float(a) for a in rows[rows.id.str.contains("2026-02")].amount) == pytest.approx(110.0)

    def test_currencies_grow_by_the_cpi_since_the_last_known_value(self, revalued, settings):
        self.write(settings, self.INFLATION, "2026-01-31;1000\n2026-02-28;estimated\n2026-03-31;estimated\n")
        usd = LedgerStore(settings).load().currencies["USD"]
        assert list(usd.base_currency_mapping) == pytest.approx([1000.0, 1100.0, 1210.0])
        assert [d.date().isoformat() for d in usd.estimated] == ["2026-02-28", "2026-03-31"]

    def test_a_currency_needs_a_known_value_before(self, settings):
        self.write(settings, self.INFLATION, "2026-02-02;estimated\n")
        with pytest.raises(LedgerDataError, match="USD estimated on 2026-02-02"):
            LedgerStore(settings).load()

    def test_a_future_estimate_revalues_nothing(self, revalued, settings):
        self.write(settings, self.INFLATION, "2026-01-05;1000\n2026-03-09;estimated\n2026-03-16;estimated\n")
        days = set(LedgerStore(settings).load().result_rows.move_date)
        assert "2026-03-09" in days
        assert "2026-03-16" not in days

    def test_editing_a_value_keeps_the_estimated_ones(self, settings):
        self.write(settings, self.INFLATION, services="2026-01;100.00;50.00\n2026-02;estimated;estimated\n")
        store = LedgerStore(settings)
        store.set_service_value("2026-02", "Gas", "70")
        assert settings.services_path.read_text().splitlines()[2] == "2026-02;estimated;70.00"
        assert service_cells(store.load())["2026-02"] == {"Rent": "estimated", "Gas": "70.00"}

    def test_the_form_saves_and_shows_estimated(self, settings):
        self.write(settings, self.INFLATION, services="2026-01;100.00;\n")
        client = TestClient(create_app(settings))
        response = client.post("/services/months", data={"month": "2026-02", "service:Rent": "estimated", "service:Gas": ""})
        assert response.status_code == 200
        assert settings.services_path.read_text().splitlines()[2] == "2026-02;estimated;"
        assert 'value="estimated"' in client.get("/services/form?month=2026-02").text


class TestWarnings(Base):
    def test_services_warn_about_past_months_only(self, settings):
        self.write(settings, "2026-01;1%\n2026-02;estimated\n2026-03;estimated\n", services=(
            "2026-01;100.00;50.00\n2026-02;estimated;50.00\n2026-03;estimated;estimated\n"
        ))
        page = TestClient(create_app(settings)).get("/services").text
        assert "1 past month still estimated" in page
        assert "<li>2026-02: Rent</li>" in page
        assert "2026-03: " not in page

    def test_no_warning_while_nothing_past_is_estimated(self, settings):
        self.write(settings, "2026-01;1%\n2026-02;2%\n2026-03;estimated\n", "2026-03-09;estimated\n2026-03-02;1000\n",
                   "2026-02;100.00;\n2026-03;estimated;\n")
        client = TestClient(create_app(settings))
        assert "still estimated" not in client.get("/services").text
        assert "still estimated" not in client.get("/currencies").text

    def test_currencies_warn_about_past_months_and_weeks(self, revalued, settings):
        self.write(settings, "2026-01;1%\n2026-02;estimated\n2026-03;estimated\n",
                   "2026-02-23;1000\n2026-03-02;estimated\n2026-03-09;estimated\n")
        page = TestClient(create_app(settings)).get("/currencies").text
        assert "1 past month still estimated" in page
        assert "<li>2026-02</li>" in page
        assert "1 past week still estimated" in page
        assert "<li>2026-03-02</li>" in page
        assert "<li>2026-03-09</li>" not in page  # the current week

    def test_estimated_months_are_projected(self, settings):
        self.write(settings, "2026-01;1%\n2026-02;estimated\n2026-03;estimated\n")
        store = LedgerStore(settings)
        data = build_currencies(store.load(), self.today)
        assert [(m["month"], m["projected"], m["estimated"]) for m in data["inflation_months"]] == [
            ("2026-01", False, False), ("2026-02", True, True), ("2026-03", True, True),
        ]
        assert data["last_month"]["month"] == "2026-01"  # the last one known


class TestFill(Base):
    def test_missing_months_and_weeks_are_added_up_to_the_current_ones(self, revalued, settings):
        self.write(settings, "2026-01;1%\n", "2026-02-16;1000\n", "2026-01;100.00;\n")
        LedgerStore(settings).fill_estimated()
        folder = settings.currencies_path.parent
        assert (folder / "base_currency.csv").read_text() == (
            "month;monthly_inflation\n2026-01;1%\n2026-02;estimated\n2026-03;estimated\n"
        )
        assert (folder / "currencies.csv").read_text() == (
            "date;USD\n2026-02-16;1000\n2026-02-23;estimated\n2026-03-02;estimated\n2026-03-09;estimated\n"
        )
        # Gas has no value in the month before: none
        assert settings.services_path.read_text() == (
            "month;Rent;Gas\n2026-01;100.00;\n2026-02;estimated;\n2026-03;estimated;\n"
        )
        LedgerStore(settings).load()

    def test_gaps_are_filled_in_their_place_keeping_the_bytes(self, settings):
        folder = settings.currencies_path.parent
        path = folder / "base_currency.csv"
        path.write_bytes(b"\xef\xbb\xbfmonth;monthly_inflation\r\n2026-01;1,5\r\n2026-03;2%")
        (folder / "currencies.csv").write_text("date;USD\n", encoding="utf-8")
        LedgerStore(settings).fill_estimated()
        assert path.read_bytes() == (
            b"\xef\xbb\xbfmonth;monthly_inflation\r\n2026-01;1,5\r\n2026-02;estimated\r\n2026-03;2%"
        )

    def test_nothing_missing_writes_nothing(self, settings):
        self.write(settings, "2026-01;1%\n2026-02;1%\n2026-03;1%\n2026-04;estimated\n", "2026-03-11;1000\n",
                   "2026-03;1.00;\n2026-05;2.00;\n")
        folder = settings.currencies_path.parent
        files = [folder / "base_currency.csv", folder / "currencies.csv", settings.services_path]
        before = [f.stat().st_mtime_ns for f in files]
        LedgerStore(settings).fill_estimated()
        assert before == [f.stat().st_mtime_ns for f in files]

    def test_files_without_rows_are_left_alone(self, settings):
        self.write(settings, "", "", "")
        LedgerStore(settings).fill_estimated()
        assert settings.services_path.read_text() == "month;Rent;Gas\n"

    def test_it_runs_on_startup_and_warns(self, settings):
        self.write(settings, "2026-01;1%\n2026-02;1%\n2026-03;1%\n", "2026-03-09;1000\n", "2026-01;100.00;\n")
        with TestClient(create_app(settings)) as client:
            assert "2 past" not in client.get("/services").text
            assert "1 past month still estimated" in client.get("/services").text
        assert "2026-03;estimated;" in settings.services_path.read_text()

    def test_a_bad_file_doesnt_stop_startup(self, settings, caplog):
        self.write(settings, "not a month;1%\n")
        with TestClient(create_app(settings)):
            pass
        assert "Could not fill in the estimates" in caplog.text
