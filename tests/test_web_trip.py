"""Tests for the Trip tab: the planned expenses of a trip and what was spent on it."""

import datetime as dt
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import HTMX

from pledger.web.app import create_app
from pledger.web.store import LedgerStore
from pledger.web.views import build_trips, ledger_weeks


@pytest.fixture
def settings(settings, tmp_path):
    return replace(settings, trips_path=tmp_path / "trips.csv", trip_items_path=tmp_path / "trip_items.csv")


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


@pytest.fixture
def store(settings):
    return LedgerStore(settings)


def view(store, name=""):
    return build_trips(store.load(), store.load_trips(), store.load_trip_items(), name)


class TestLedgerWeeks:
    @pytest.mark.parametrize(("start", "end", "weeks"), [
        ("2026-10-14", "2026-10-18", ("2026-10-12", "2026-10-19")),  # Wednesday to Sunday
        ("2026-10-12", "2026-10-19", ("2026-10-12", "2026-10-19")),  # both Mondays
    ])
    def test_weeks(self, start, end, weeks):
        assert ledger_weeks(start, end) == weeks


class TestTrips:
    def test_spent_is_of_its_base_movement_in_its_weeks(self, store):
        store.add("2026-10-12", "Food", "Eq", "Ca", "10", "first Monday")
        store.add("2026-10-19", "Food", "Eq", "Ca", "20", "last Monday")
        store.add("2026-10-11", "Food", "Eq", "Ca", "100", "before")
        store.add("2026-10-20", "Food", "Eq", "Ca", "100", "after")
        store.add("2026-10-14", "Card fee", "Eq", "Cc", "100", "another base movement")
        store.save_trip("Rome", "2026-10-14", "2026-10-18", "Food")
        trip = view(store)["trip"]
        assert trip["spent"] == 30 and [m["comment"] for m in trip["movements"]] == ["last Monday", "first Monday"]

    def test_without_a_base_movement_nothing_is_spent(self, store):
        store.save_trip("Rome", "2026-10-14", "2026-10-18")
        assert view(store)["trip"]["spent"] == 0

    def test_planned_and_left(self, store):
        store.save_trip("Rome", "2026-10-14", "2026-10-18", "Food")
        store.add("2026-10-14", "Food", "Eq", "Ca", "30", "")
        store.add_trip_item("Rome", "Flights", "there", "100", "ARS")
        store.add_trip_item("Rome", "Lodging", "", "50", "ARS")
        result = view(store)
        trip = result["trip"]
        assert (trip["planned"], trip["left"], trip["days"], trip["per_day"]) == (150, 120, 5, 30)
        assert [(c["name"], c["value"]) for c in result["categories"]] == [("Flights", 100), ("Lodging", 50)]

    def test_monthly_before_it_starts(self, store):
        today = store.load().today
        start = (today.replace(day=1) + dt.timedelta(days=100)).replace(day=1)  # three calendar months away
        store.save_trip("Rome", start.isoformat(), (start + dt.timedelta(days=3)).isoformat())
        store.add_trip_item("Rome", "Flights", "", "300", "ARS")
        trip = view(store)["trip"]
        assert trip["monthly"] == pytest.approx(100) and trip["status"] == "Upcoming"

    def test_picks_the_next_one_by_default(self, store):
        today = store.load().today
        store.save_trip("Past", "2020-01-01", "2020-01-02")
        store.save_trip("Next", (today + dt.timedelta(days=9)).isoformat(), (today + dt.timedelta(days=10)).isoformat())
        assert view(store)["trip"]["name"] == "Next" and view(store, "Past")["trip"]["name"] == "Past"

    def test_foreign_expense_at_todays_value(self, revalued, tmp_path):
        settings = replace(revalued, trips_path=tmp_path / "trips.csv", trip_items_path=tmp_path / "trip_items.csv")
        (tmp_path / "currencies.csv").write_text("date;USD\n2026-01-01;1000\n", encoding="utf-8")
        store = LedgerStore(settings)
        store.save_trip("Rome", "2026-10-14", "2026-10-18")
        store.add_trip_item("Rome", "Flights", "", "2", "USD")
        result = view(store)
        assert result["trip"]["planned"] == 2000 and result["items"][0]["amount"] == 2

    def test_same_name_updates_and_keeps_its_expenses(self, store):
        store.save_trip("Rome", "2026-10-14", "2026-10-18")
        store.add_trip_item("Rome", "Flights", "", "100", "ARS")
        store.save_trip("Rome", "2026-11-14", "2026-11-18")
        assert len(store.load_trips()) == 1 and view(store)["trip"]["start"] == "2026-11-14" and len(view(store)["items"]) == 1

    def test_delete_takes_its_expenses_only(self, store):
        store.save_trip("Rome", "2026-10-14", "2026-10-18")
        store.save_trip("Oslo", "2026-11-14", "2026-11-18")
        store.add_trip_item("Rome", "Flights", "", "100", "ARS")
        store.add_trip_item("Oslo", "Flights", "", "100", "ARS")
        store.delete_trip("Rome")
        assert list(store.load_trip_items()["trip"]) == ["Oslo"]

    def test_delete_item_by_id(self, store):
        store.save_trip("Rome", "2026-10-14", "2026-10-18")
        for amount in ("1", "2", "3"):
            store.add_trip_item("Rome", "Flights", "", amount, "ARS")
        store.delete_trip_item("2")
        store.add_trip_item("Rome", "Flights", "", "4", "ARS")
        assert list(store.load_trip_items()["id"]) == ["1", "3", "4"]

    @pytest.mark.parametrize(("name", "start", "end", "base", "error"), [
        ("", "2026-01-01", "2026-01-02", "", "name is required"),
        ("Rome", "", "2026-01-02", "", "dates are required"),
        ("Rome", "2026-01-03", "2026-01-02", "", "cannot end before"),
        ("Rome", "nope", "2026-01-02", "", "Invalid start date"),
        ("Rome", "2026-01-01", "2026-01-02", "Salary", "Pick a loss base movement"),
    ])
    def test_invalid_trip(self, store, name, start, end, base, error):
        with pytest.raises(ValueError, match=error):
            store.save_trip(name, start, end, base)

    @pytest.mark.parametrize(("trip", "amount", "currency", "error"), [
        ("Nope", "1", "ARS", "No trip named"),
        ("Rome", "", "ARS", "amount is required"),
        ("Rome", "-1", "ARS", "cannot be negative"),
        ("Rome", "1", "XXX", "Unknown currency"),
    ])
    def test_invalid_item(self, store, trip, amount, currency, error):
        store.save_trip("Rome", "2026-01-01", "2026-01-02")
        with pytest.raises(ValueError, match=error):
            store.add_trip_item(trip, "Flights", "", amount, currency)

    def test_page(self, client, settings):
        ledger = settings.ledger_path.read_bytes()
        assert "No trips yet" in client.get("/trip").text
        r = client.post("/trip/trips", data={"name": "Rome", "start": "2026-10-14", "end": "2026-10-18", "base_movement": "Food"}, headers=HTMX)
        assert "Saved the trip Rome" in r.text and "<!doctype" not in r.text
        r = client.post("/trip/items", data={"trip": "Rome", "category": "Flights", "description": "there", "amount": "100", "currency": "ARS"}, headers=HTMX)
        assert "Added the expense" in r.text and '<td class="muted money">there</td>' in r.text and "$100.00" in r.text
        r = client.post("/trip/items", data={"trip": "Rome", "category": "Flights", "amount": "x", "currency": "ARS"}, headers=HTMX)
        assert 'class="msg error"' in r.text and 'value="x"' in r.text
        r = client.post("/trip/items/delete", data={"trip": "Rome", "item": "1"}, headers=HTMX)
        assert "Nothing planned yet" in r.text
        r = client.post("/trip/trips/delete", data={"name": "Rome"}, headers=HTMX)
        assert "No trips yet" in r.text
        assert settings.ledger_path.read_bytes() == ledger
        assert '<a href="/trip"' in client.get("/").text
