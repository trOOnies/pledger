"""Tests for pledger.web."""

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from pledger.web.app import create_app
from pledger.web.store import LedgerStore, Settings, _insert_yaml_entry, row_fingerprint
from pledger.web.templating import money

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


class TestMoney:
    @pytest.mark.parametrize(
        ("value", "currency", "expected"),
        [
            (1000.5, "$", "$1,000.50"),
            (-50.255, "$", "-$50.26"),
            (0, "$", "$0.00"),
            (-0.001, "$", "$0.00"),
            (1234, "ARS", "ARS 1,234.00"),
        ],
    )
    def test_money(self, value, currency, expected):
        assert money(value, currency) == expected


class TestSetupStore:
    def test_add_account_keeps_the_rest_of_the_file(self, settings):
        path = settings.accounts_path
        path.write_text(
            "# my accounts\nasset:\n  Ca:\n    long_name: Cash (A)\n    description: Cash\n"
            "  Bk:\n    description: Bank account\n\nliability:\n  Cc:\n    description: Credit card\n"
            "equity:\n  Eq:\n    description: Equity\n"
        )
        before = path.read_text()

        LedgerStore(settings).add_account("Sv", "asset", "Savings: 10%")

        lines = before.splitlines()
        assert path.read_text().splitlines() == (
            lines[:7] + ["  Sv:", "    description: 'Savings: 10%'"] + lines[7:]
        )
        assert LedgerStore(settings).load().accounts["Sv"].description == "Savings: 10%"

    def test_insert_yaml_entry_into_missing_section(self):
        text = "asset:\n  Ca:\n    description: Cash\n\n"
        out = _insert_yaml_entry(text, "liability", "Cc", {"description": ""})
        assert out == "asset:\n  Ca:\n    description: Cash\nliability:\n  Cc:\n    description: ''\n"

    def test_insert_yaml_entry_keeps_crlf_and_indent(self):
        text = "gain:\r\n    A:\r\n        description: a\r\nloss: {}\r\n"
        out = _insert_yaml_entry(text, "gain", "B", {"description": "b"})
        assert out == "gain:\r\n    A:\r\n        description: a\r\n    B:\r\n        description: b\r\nloss: {}\r\n"
        with pytest.raises(ValueError, match="edit the file by hand"):
            _insert_yaml_entry(text, "loss", "C", {"description": "c"})

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (("Ca", "asset"), "already exists"),
            (("  ", "asset"), "name is required"),
            (("A;B", "asset"), "cannot contain"),
            (("Sv", "nope"), "Invalid account type"),
            (("Eq2", "equity"), "equity accounts are not supported"),
        ],
    )
    def test_add_account_rejects_invalid(self, settings, args, message):
        before = settings.accounts_path.read_text()
        with pytest.raises(ValueError, match=message):
            LedgerStore(settings).add_account(*args)
        assert settings.accounts_path.read_text() == before

    def test_add_base_movement(self, settings):
        store = LedgerStore(settings)
        store.add_base_movement("Rent", "loss", "Monthly rent", debit="Ca", credit="Bk")
        store.add_base_movement("ATM", "transfer", "", debit="Ca", credit="Bk")
        bases = store.load().base_movements
        rent = bases["Rent"]  # debit is forced to equity for losses
        assert (rent.acc_debit.name, rent.acc_credit.name) == ("Eq", "Bk")
        assert rent.move_type.value == "loss"
        assert (bases["ATM"].acc_debit.name, bases["ATM"].acc_credit.name) == ("Ca", "Bk")
        data = yaml.safe_load(settings.base_movements_path.read_text())
        assert data["loss"]["Rent"] == {"description": "Monthly rent", "credit": "Bk"}

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (("Food", "loss"), "already exists"),
            (("X", "nope"), "Invalid movement type"),
            (("X", "transfer", "", "Ca", "Ca"), "must be different"),
            (("X", "transfer", "", "Eq"), "non-selectable account"),
            (("X", "gain", "", "Nope"), "non-selectable account"),
        ],
    )
    def test_add_base_movement_rejects_invalid(self, settings, args, message):
        before = settings.base_movements_path.read_text()
        with pytest.raises(ValueError, match=message):
            LedgerStore(settings).add_base_movement(*args)
        assert settings.base_movements_path.read_text() == before


class TestPages:
    def test_index(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "New movement" in r.text
        assert "$1,000.50" in r.text
        assert "$960.25" in r.text  # profit card

    def test_currency_setting(self, settings):
        client = TestClient(create_app(Settings(*list(vars(settings).values())[:3], "ARS")))
        assert "ARS 1,000.50" in client.get("/").text

    def test_index_partial_for_htmx(self, client):
        r = client.get("/", params={"month": "2026-01", "account": "Ca"}, headers=HTMX)
        assert r.status_code == 200
        assert "<html" not in r.text
        assert "Salary" not in r.text.split("<tbody>")[1]  # Salary doesn't touch Ca

    def test_form_picks_type_then_base_movement(self, client):
        r = client.get("/")
        form = r.text.split('id="movement-form"')[1].split("</form>")[0]
        assert '<select name="move_type"' in form
        base_select = form.split('name="base_movement"')[1].split("</select>")[0]
        assert "Gift" in base_select and "Food" not in base_select  # gain only

    def test_form_selects_swap_their_own_container(self, client):
        # The form swaps outerHTML; without an explicit swap the selects would
        # inherit it and replace (and lose) their target containers.
        form = client.get("/").text
        assert 'hx-target="#base-fields" hx-swap="innerHTML"' in form
        assert 'hx-target="#account-fields" hx-swap="innerHTML"' in form
        assert 'Comment <em class="optional">(optional)</em>' in form

    def test_form_bases_for_type(self, client):
        r = client.get("/form/bases", params={"move_type": "transfer"})
        assert "<option selected>Move</option>" in r.text
        assert "Withdraw" in r.text and "Food" not in r.text
        assert '<select name="acc_debit"' in r.text  # Move has free sides

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
        assert "Added Gift: $5.00" in r.text
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
        assert "New account" in r.text and "New base movement" in r.text
        new_account = r.text.split('hx-post="/setup/accounts"')[1].split("</form>")[0]
        assert 'value="equity"' not in new_account
        assert 'value="asset"' in new_account
        # 2 descriptions + credit (the default loss type fixes debit to equity)
        assert r.text.count('<em class="optional">(optional)</em>') == 3

    def test_tables_are_sortable(self, client):
        index, setup = client.get("/").text, client.get("/setup").text
        assert 'data-sortable="movements"' in index
        assert 'data-sortable="balances-asset"' in index
        assert 'data-sort="1000.5"' in index  # sort by raw amount, not formatted text
        assert 'data-sortable="setup-accounts-asset"' in setup
        assert 'data-sortable="setup-bases-gain"' in setup

    def test_setup_create_account(self, client, settings):
        r = client.post(
            "/setup/accounts", data={"name": "Sv", "acc_type": "asset", "description": "Savings"},
            headers=HTMX,
        )
        assert "Added account Sv" in r.text
        assert ">Sv</option>" in r.text  # selectable for new base movements
        assert "Sv" in LedgerStore(settings).load().accounts

        r = client.post("/setup/accounts", data={"name": "Sv", "acc_type": "asset"}, headers=HTMX)
        assert 'class="msg error"' in r.text and "already exists" in r.text

    def test_setup_create_base_movement(self, client, settings):
        data = {"name": "Rent", "move_type": "loss", "description": "", "credit": "Bk"}
        r = client.post("/setup/base-movements", data=data, headers=HTMX)
        assert "Added base movement Rent" in r.text
        assert "Rent" in LedgerStore(settings).load().base_movements
        r = client.get("/form/bases", params={"move_type": "loss"})
        assert "Rent" in r.text

    def test_form_base_sides(self, client):
        r = client.get("/form/base-sides", params={"move_type": "gain"})
        assert '<select name="debit"' in r.text
        assert 'name="credit"' not in r.text and 'value="Eq"' in r.text

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

    def test_create_account_and_base_movement(self, client):
        body = {"name": "Sv", "type": "asset", "description": "Savings"}
        assert client.post("/api/accounts", json=body).status_code == 201
        assert client.post("/api/accounts", json=body).status_code == 422
        equity = {"name": "Eq2", "type": "equity"}
        assert client.post("/api/accounts", json=equity).status_code == 422
        body = {"name": "Deposit", "type": "transfer", "debit": "Sv", "credit": "Ca"}
        assert client.post("/api/base-movements", json=body).status_code == 201
        bases = {b["name"]: b for b in client.get("/api/base-movements").json()}
        assert bases["Deposit"]["debit"] == "Sv"
