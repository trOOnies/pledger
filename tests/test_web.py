"""Tests for pledger.web."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pledger.web.app import create_app
from pledger.web.store import LedgerStore, Settings, row_fingerprint

HTMX = {"HX-Request": "true"}


@pytest.fixture
def settings(accounts_yaml, base_movements_yaml, ledger_csv) -> Settings:
    return Settings(Path(accounts_yaml), Path(base_movements_yaml), Path(ledger_csv))


@pytest.fixture
def client(settings) -> TestClient:
    return TestClient(create_app(settings))


def rows(settings):
    return LedgerStore(settings).load().rows


class TestStore:
    def test_add_appends_one_line_and_keeps_the_rest(self, settings):
        path = settings.ledger_path
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n").rstrip(b"\r\n"))
        before = path.read_bytes()

        LedgerStore(settings).add("2026-02-01", "Food", "Eq", "Bk", "1234.5", "x; y")

        assert path.read_bytes() == before + b"\r\n2026-02-01;Food;Eq;1,234.50;Bk;x, y"

    def test_delete_removes_only_that_line(self, settings):
        path = settings.ledger_path
        before = path.read_text().splitlines()
        store = LedgerStore(settings)
        store.delete(1, row_fingerprint(store.load().rows.iloc[1]))
        assert path.read_text().splitlines() == before[:2] + before[3:]

    def test_delete_with_stale_fingerprint_is_rejected(self, settings):
        from pledger.web.store import StaleRowError

        with pytest.raises(StaleRowError):
            LedgerStore(settings).delete(0, "2000-01-01|Food|1")
        assert len(rows(settings)) == 4

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (("2026-13-01", "Food", "Eq", "Ca", "1"), "Invalid date"),
            (("2026-01-01", "Nope", "Eq", "Ca", "1"), "Unknown base movement"),
            (("2026-01-01", "Food", "Eq", "Nope", "1"), "Unknown account"),
            (("2026-01-01", "Food", "Eq", "Ca", "abc"), "Invalid amount"),
            (("2026-01-01", "Food", "Ca", "Bk", "1"), "Movement type must match"),
            (("2026-01-01", "Salary", "Ca", "Eq", "1"), "Debit account must match"),
        ],
    )
    def test_add_rejects_invalid_movements(self, settings, args, message):
        with pytest.raises(ValueError, match=message):
            LedgerStore(settings).add(*args)
        assert len(rows(settings)) == 4


class TestPages:
    def test_index(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "New movement" in r.text
        assert "1,000.50" in r.text

    def test_index_partial_for_htmx(self, client):
        r = client.get("/", params={"month": "2026-01", "account": "Ca"}, headers=HTMX)
        assert r.status_code == 200
        assert "<html" not in r.text
        assert "Salary" not in r.text.split("<tbody>")[1]  # Salary doesn't touch Ca

    def test_form_accounts_fixed_and_free_sides(self, client):
        r = client.get("/form/accounts", params={"base_movement": "Salary"})
        assert 'name="acc_debit" value="Bk"' in r.text
        r = client.get("/form/accounts", params={"base_movement": "Gift"})
        assert '<select name="acc_debit"' in r.text
        assert 'name="acc_credit" value="Eq"' in r.text

    def test_create_movement(self, client, settings):
        form = {
            "move_date": "2026-02-01", "base_movement": "Gift",
            "acc_debit": "Ca", "acc_credit": "Eq", "amount": "5",
        }
        r = client.post("/movements", data=form, headers=HTMX)
        assert r.status_code == 200
        assert r.headers["HX-Trigger"] == "ledger-changed"
        assert "Added Gift" in r.text
        assert len(rows(settings)) == 5

    def test_create_invalid_movement_shows_error(self, client, settings):
        form = {
            "move_date": "2026-02-01", "base_movement": "Gift",
            "acc_debit": "Eq", "acc_credit": "Eq", "amount": "5",
        }
        r = client.post("/movements", data=form, headers=HTMX)
        assert "HX-Trigger" not in r.headers
        assert 'class="msg error"' in r.text
        assert len(rows(settings)) == 4

    def test_delete_movement(self, client, settings):
        fp = row_fingerprint(rows(settings).iloc[0])
        r = client.post("/movements/0/delete", data={"fingerprint": fp})
        assert r.status_code == 200
        assert len(rows(settings)) == 3
        r = client.post("/movements/0/delete", data={"fingerprint": fp})
        assert r.status_code == 409

    def test_setup(self, client):
        r = client.get("/setup")
        assert r.status_code == 200
        assert "Withdraw" in r.text

    def test_broken_ledger_shows_error_page(self, client, settings):
        settings.ledger_path.write_text(
            "move_date;base_movement;acc_debit;amount;acc_credit\n2026-01-01;Nope;Ca;1;Eq\n"
        )
        r = client.get("/")
        assert r.status_code == 500
        assert "Could not load the ledger" in r.text


class TestApi:
    def test_summary(self, client):
        data = client.get("/api/summary").json()
        assert data["profit"] == pytest.approx(960.25)
        assert data["balances"]["Bk"] == pytest.approx(800.50)

    def test_summary_balances_are_cumulative_by_month(self, client, settings):
        LedgerStore(settings).add("2026-03-01", "Gift", "Ca", "Eq", "100")
        jan = client.get("/api/summary", params={"month": "2026-01"}).json()
        mar = client.get("/api/summary", params={"month": "2026-03"}).json()
        assert jan["profit"] == pytest.approx(960.25)
        assert mar["profit"] == pytest.approx(100)
        assert mar["balances"]["Ca"] == pytest.approx(jan["balances"]["Ca"] + 100)

    def test_movements_and_metadata(self, client):
        movements = client.get("/api/movements").json()
        assert movements[0]["date"] == "2026-01-04"  # newest first
        assert len(client.get("/api/accounts").json()) == 4
        bases = {b["name"]: b for b in client.get("/api/base-movements").json()}
        assert bases["Withdraw"]["debit"] == "Ca"

    def test_create_and_delete(self, client, settings):
        body = {
            "move_date": "2026-02-01", "base_movement": "Move",
            "acc_debit": "Bk", "acc_credit": "Cc", "amount": 10,
        }
        assert client.post("/api/movements", json=body).status_code == 201
        assert client.delete("/api/movements/4").status_code == 200
        assert len(rows(settings)) == 4
        body["acc_credit"] = "Eq"
        assert client.post("/api/movements", json=body).status_code == 422
