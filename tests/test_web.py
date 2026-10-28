"""Tests for pledger.web."""

import datetime as dt
import html
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from pledger.enums import AccountCheck, InvestmentType
from pledger.models import DEBT, INVESTMENT, CreditCardAccount
from pledger.web.app import create_app
from pledger.web.store import (
    LedgerDataError,
    LedgerStore,
    Settings,
    StaleRowError,
    _insert_yaml_entry,
    conversion_fingerprint,
    deduction_id,
    interest_fingerprint,
    row_fingerprint,
    work_fingerprint,
)
from pledger.web.templating import money, quantity
from pledger.web.views import (
    build_accounts,
    build_credit_cards,
    build_currencies,
    build_debts,
    build_investments,
    build_results,
    build_view,
    column_chart,
    composition,
    equity_series,
    investment_charts,
    nice_ticks,
    percent,
)

HTMX = {"HX-Request": "true"}

# The internal base movements required to load the ledger
INITIAL_YAML = (
    "internal:\n  Init:\n    id: 3\n    long_name: Initial\n    description: d\n"
    "    base_movements:\n      gain:\n        Initial (G): Initial balance\n"
    "      loss:\n        Initial (L): Initial balance\n"
)
# The internal base movements of the results of the accounts in other currencies,
# required once one is revalued; appended to the `internal` section of INITIAL_YAML
RESULT_YAML = (
    "  Expo:\n    id: 4\n    long_name: Exposition\n    description: d\n"
    "    base_movements:\n      gain:\n        USD - Result (G): USD gains\n"
    "      loss:\n        USD - Result (L): USD losses\n"
)


@pytest.fixture
def settings(accounts_yaml, base_movements_yaml, ledger_csv, currencies_yaml) -> Settings:
    # At the top, so that later appends extend the `transfer` section
    path = Path(base_movements_yaml)
    path.write_text(INITIAL_YAML + path.read_text())
    return Settings(
        Path(accounts_yaml), Path(base_movements_yaml), Path(ledger_csv), Path(currencies_yaml)
    )


@pytest.fixture
def revalued(settings) -> Settings:
    """The settings, with the base movements to revalue the accounts in other currencies."""
    path = settings.base_movements_path
    path.write_text(path.read_text().replace(INITIAL_YAML, INITIAL_YAML + RESULT_YAML, 1))
    return settings


@pytest.fixture
def client(settings) -> TestClient:
    return TestClient(create_app(settings))


def movement_ids(text: str) -> list[str]:
    """The ids in the ID column of the tables of movements of a page, in order."""
    return re.findall(r'<td class="nowrap muted small" data-sort="([^"]+)">', text)


def rows(settings):
    return LedgerStore(settings).load().rows


class TestStore:
    def test_add_appends_one_line_and_keeps_the_rest(self, settings):
        path = settings.ledger_path
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n").rstrip(b"\r\n"))
        store = LedgerStore(settings)
        store.freeze_ids()
        before = path.read_bytes()

        store.add("2026-02-01", "Food", "Eq", "Bk", "1234.5", "x; y")

        assert path.read_bytes() == before + b"\r\n2026-02-01;Food;Eq;1,234.50;Bk;x, y;L-5"

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


class TestIds:
    def test_frozen_on_startup_keeping_every_other_byte(self, settings):
        path = settings.ledger_path
        before = path.read_text().splitlines()
        with TestClient(create_app(settings)):  # runs the startup
            pass
        assert path.read_text().splitlines() == [
            before[0] + ";id", *(f"{line};L-{i}" for i, line in enumerate(before[1:], 1))
        ]
        frozen = path.read_bytes()
        LedgerStore(settings).freeze_ids()
        assert path.read_bytes() == frozen

    def test_missing_malformed_and_repeated_ones_get_new_ids(self, settings):
        path = settings.ledger_path
        lines = path.read_text().splitlines()
        # A row copied with its id, one with a made-up id and one added without any
        path.write_text("\n".join([
            lines[0] + ";id", lines[1] + ";L-7", lines[2] + ";L-7", lines[3] + ";nope", lines[4],
        ]) + "\n")
        LedgerStore(settings).freeze_ids()
        assert [line.rsplit(";", 1)[1] for line in path.read_text().splitlines()] == ["id", "L-7", "L-8", "L-9", "L-10"]

    def test_unchanged_by_filters_and_by_deleting_other_rows(self, settings):
        store = LedgerStore(settings)
        store.freeze_ids()
        by_fingerprint = {m["fingerprint"]: m["id"] for m in build_view(store.load(), initial=True)["movements"]}
        assert len(set(by_fingerprint.values())) == len(by_fingerprint)
        for view in (build_view(store.load(), month="2026-01"), build_view(store.load(), account="Ca")):
            assert all(by_fingerprint[m["fingerprint"]] == m["id"] for m in view["movements"])
        store.delete(1, row_fingerprint(store.load().rows.iloc[1]))
        assert [m["id"] for m in build_view(store.load())["movements"]] == ["L-4", "L-3", "L-1"]
        # A new row never takes the id of one deleted before it
        store.add("2026-02-01", "Food", "Eq", "Ca", "1")
        assert build_view(store.load())["movements"][0]["id"] == "L-5"

    def test_rows_added_while_running_show_none_until_frozen(self, settings, client):
        LedgerStore(settings).freeze_ids()
        with settings.ledger_path.open("a") as f:
            f.write("2026-01-05;Food;Eq;1;Ca;\n")
        movements = client.get("/api/movements").json()
        assert [m["id"] for m in movements] == [None, "L-4", "L-3", "L-2", "L-1"]
        assert 'title="Added to the CSV while pledger runs' in client.get("/").text
        # Adding a movement freezes it first; an id sent by the client is ignored
        body = {"move_date": "2026-01-06", "base_movement": "Food", "acc_debit": "Eq",
                "acc_credit": "Ca", "amount": 2, "id": "L-1"}
        assert client.post("/api/movements", json=body).status_code == 201
        assert [m["id"] for m in client.get("/api/movements").json()][:2] == ["L-6", "L-5"]


class TestAccountChecks:
    @pytest.fixture
    def settings(self, settings):
        path = settings.accounts_path
        path.write_text(path.read_text().replace(
            "    initial_amount: 0.0\n", "    initial_amount: 0.0\n    checks:\n    - net_positive\n", 1
        ))
        return settings

    def test_load_checks_the_ledger(self, settings):
        with settings.ledger_path.open("a") as f:
            f.write("2026-01-05;Food;Eq;160;Ca;\n")
        with pytest.raises(LedgerDataError, match="'Ca' breaks its net_positive check: .* -0.25 at the end of 2026-01-05"):
            LedgerStore(settings).load()

    def test_add_breaking_a_check_is_rejected(self, settings):
        store = LedgerStore(settings)
        store.add("2026-01-05", "Food", "Eq", "Ca", "159.75")  # down to 0.00
        with pytest.raises(ValueError, match="net_positive"):
            store.add("2026-01-05", "Food", "Eq", "Ca", "0.01")
        assert len(rows(settings)) == 5

    def test_delete_breaking_a_check_is_rejected(self, settings):
        store = LedgerStore(settings)
        withdraw = store.load().rows.iloc[1]  # the 200 that Ca spends from
        with pytest.raises(ValueError, match="-50.25 at the end of 2026-01-03"):
            store.delete(1, row_fingerprint(withdraw))
        assert len(rows(settings)) == 4


class TestInternalBaseMovements:
    @pytest.fixture
    def settings(self, settings):
        # An internal transfer, with one movement at the end of the ledger
        path = settings.base_movements_path
        path.write_text(path.read_text().replace("internal:\n", "internal:\n  transfer:\n    Adjust: Set by pledger\n", 1))
        with settings.ledger_path.open("a") as f:
            f.write("2026-01-05;Adjust;Bk;5;Ca;\n")
        return settings

    def test_cannot_be_added(self, settings, client):
        with pytest.raises(ValueError, match="internal"):
            LedgerStore(settings).add("2026-02-01", "Adjust", "Bk", "Ca", "1")
        body = {
            "move_date": "2026-02-01", "base_movement": "Adjust",
            "acc_debit": "Bk", "acc_credit": "Ca", "amount": 1,
        }
        assert client.post("/api/movements", json=body).status_code == 422
        assert len(rows(settings)) == 5

    def test_cannot_be_picked_in_the_form(self, client):
        r = client.get("/form/bases", params={"move_type": "transfer"})
        assert "Move" in r.text and "Adjust" not in r.text
        # Not even when submitted by name
        data = {"move_date": "2026-02-01", "move_type": "transfer", "base_movement": "Adjust",
                "acc_debit": "Bk", "acc_credit": "Ca", "amount": "1"}
        r = client.post("/movements", data=data, headers=HTMX)
        assert "is internal" in r.text and ">Adjust<" not in r.text

    def test_cannot_be_deleted(self, settings, client):
        fp = row_fingerprint(rows(settings).iloc[4])
        with pytest.raises(ValueError, match="cannot be deleted"):
            LedgerStore(settings).delete(4, fp)
        assert client.post("/movements/4/delete", data={"fingerprint": fp}).status_code == 422
        assert client.delete("/api/movements/4").status_code == 422
        assert len(rows(settings)) == 5
        # The ledger shows a lock instead of the delete button
        index = client.get("/").text
        assert index.count('aria-label="Delete movement"') == 4
        assert "cannot be deleted by hand" in index
        assert client.get("/api/base-movements").json()[-1] == {
            "name": "Adjust", "description": "Set by pledger", "type": "transfer",
            "result_type": None, "debit": None, "credit": None, "internal": True,
        }


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
            "    initial_amount: 0\n  Bk:\n    description: Bank account\n    initial_amount: 0\n"
            "\nliability:\n  Cc:\n    description: Credit card\n    initial_amount: 0\n"
            "equity:\n  Eq:\n    description: Equity\n"
        )
        before = path.read_text()

        LedgerStore(settings).add_account("Sv", "asset", "Savings: 10%", 0.0)

        lines = before.splitlines()
        assert path.read_text().splitlines() == (
            lines[:9] + [
                "  Sv:", "    description: 'Savings: 10%'", "    currency: ARS",
                "    is_liquid: false", "    initial_amount: 0.0",
            ] + lines[9:]
        )
        sv = LedgerStore(settings).load().accounts["Sv"]
        assert sv.description == "Savings: 10%"
        assert (sv.currency.name, sv.is_liquid, sv.initial_amount, sv.initial_date) == (
            "ARS", False, 0.0, None
        )

    def test_add_account_with_currency_liquidity_and_initial_values(self, settings):
        LedgerStore(settings).add_account(
            "Sv", "asset", "Savings", "1,234.5", "USD", True, "2026-01-15"
        )
        assert "    initial_date: 2026-01-15" in settings.accounts_path.read_text()
        sv = LedgerStore(settings).load().accounts["Sv"]
        assert (sv.currency.name, sv.is_liquid, sv.initial_amount, sv.initial_date) == (
            "USD", True, 1234.5, dt.date(2026, 1, 15)
        )

    def test_add_account_with_checks(self, settings):
        store = LedgerStore(settings)
        store.add_account("Fi", "asset", "Fictitious", 0, checks=["net_zero"])
        assert "  Fi:\n    description: Fictitious\n" in settings.accounts_path.read_text()
        assert "    checks:\n    - net_zero\n" in settings.accounts_path.read_text()
        assert store.load().accounts["Fi"].checks == (AccountCheck.NET_ZERO,)

    @pytest.mark.parametrize(
        ("amount", "checks", "message"),
        [
            (0, ["positive"], "Invalid checks"),
            (0, ["net_zero", "net_zero"], "Repeated checks"),
            (5, ["net_zero"], "breaks its net_zero check: its balance is 5.00"),  # its initial movement
        ],
    )
    def test_add_account_rejects_invalid_checks(self, settings, amount, checks, message):
        before = settings.accounts_path.read_text()
        with pytest.raises(ValueError, match=message):
            LedgerStore(settings).add_account("Fi", "asset", "Fictitious", amount, checks=checks)
        assert settings.accounts_path.read_text() == before

    def test_insert_yaml_entry_into_missing_section(self):
        text = "asset:\n  Ca:\n    description: Cash\n\n"
        out = _insert_yaml_entry(text, ("liability",), "Cc", {"description": ""})
        assert out == "asset:\n  Ca:\n    description: Cash\nliability:\n  Cc:\n    description: ''\n"

    def test_insert_yaml_entry_keeps_crlf_and_indent(self):
        text = "gain:\r\n    A:\r\n        description: a\r\nloss: {}\r\n"
        out = _insert_yaml_entry(text, ("gain",), "B", {"description": "b"})
        assert out == "gain:\r\n    A:\r\n        description: a\r\n    B:\r\n        description: b\r\nloss: {}\r\n"
        with pytest.raises(ValueError, match="edit the file by hand"):
            _insert_yaml_entry(text, ("loss",), "C", {"description": "c"})

    def test_insert_yaml_entry_nested(self):
        text = (
            "rt:\n  A:\n    bm:\n      gain:\n        G:\n          description: g\n"
            "  B:\n    id: 2\n    bm:\n      gain:\n"
            "transfer:\n  T:\n    description: t\n"
        )
        out = _insert_yaml_entry(text, ("rt", "A", "bm", "gain"), "G2", {"description": "x"})
        assert "        G:\n          description: g\n        G2:\n          description: x\n  B:" in out
        # Missing keys are created at the end of their parent block
        out = _insert_yaml_entry(text, ("rt", "A", "bm", "loss"), "L", {"description": "x"})
        assert "description: g\n      loss:\n        L:\n          description: x\n  B:" in out
        # Empty (null) sections get their first entry
        out = _insert_yaml_entry(text, ("rt", "B", "bm", "gain"), "G3", {"description": "x"})
        assert "      gain:\n        G3:\n          description: x\ntransfer:" in out
        # Only direct children match: "gain" under A is not a key of "rt"
        out = _insert_yaml_entry(text, ("rt", "gain"), "X", {"description": "x"})
        assert out.endswith("  gain:\n    X:\n      description: x\ntransfer:\n  T:\n    description: t\n")
        assert yaml.safe_load(out)["rt"]["A"]["bm"]["gain"] == {"G": {"description": "g"}}

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (("Ca", "asset", "d", 0), "already exists"),
            (("  ", "asset", "d", 0), "name is required"),
            (("A;B", "asset", "d", 0), "cannot contain"),
            (("Sv", "nope", "d", 0), "Invalid account type"),
            (("Eq2", "equity", "d", 0), "equity accounts are not supported"),
            (("Sv", "asset", "  ", 0), "description is required"),
            (("Sv", "asset", "d", 0, "EUR"), "Unknown currency"),
            (("Sv", "asset", "d", "-1"), "cannot be negative"),
            (("Sv", "asset", "d", "abc"), "Invalid initial amount"),
            (("Sv", "asset", "d", "  "), "initial amount is required"),
            (("Sv", "asset", "d", 0, "", False, "2026-13-01"), "Invalid initial date"),
        ],
    )
    def test_add_account_rejects_invalid(self, settings, args, message):
        before = settings.accounts_path.read_text()
        with pytest.raises(ValueError, match=message):
            LedgerStore(settings).add_account(*args)
        assert settings.accounts_path.read_text() == before

    def test_add_base_movement(self, settings):
        store = LedgerStore(settings)
        store.add_base_movement(
            "Rent", "loss", "Monthly rent", debit="Ca", credit="Bk", result_type="Op"
        )
        store.add_base_movement(
            "ATM", "transfer", "Cash withdrawal", debit="Ca", credit="Bk", result_type="Op"
        )
        store.add_base_movement("Gift out", "loss", "Gift to others", result_type="Gifts")
        bases = store.load().base_movements
        rent = bases["Rent"]  # debit is forced to equity for losses
        assert (rent.acc_debit.name, rent.acc_credit.name) == ("Eq", "Bk")
        assert rent.move_type.value == "loss"
        assert rent.result_type.name == "Op"
        assert (bases["ATM"].acc_debit.name, bases["ATM"].acc_credit.name) == ("Ca", "Bk")
        assert bases["ATM"].result_type is None  # transfers ignore the result type
        assert bases["Gift out"].result_type.name == "Gifts"
        data = yaml.safe_load(settings.base_movements_path.read_text())
        op = data["result_types"]["Op"]["base_movements"]
        assert op["loss"]["Rent"] == {"description": "Monthly rent", "credit": "Bk"}
        assert data["transfer"]["ATM"] == {"description": "Cash withdrawal", "debit": "Ca", "credit": "Bk"}
        gifts = data["result_types"]["Gifts"]["base_movements"]
        assert gifts["loss"] == {"Gift out": {"description": "Gift to others"}}

    def test_add_result_type(self, settings):
        store = LedgerStore(settings)
        store.add_result_type(" Tax ", "Taxes", "Taxes paid")
        result_types = store.load().result_types
        assert (result_types[4].name, result_types[4].long_name) == ("Tax", "Taxes")
        data = yaml.safe_load(settings.base_movements_path.read_text())
        assert data["result_types"]["Tax"] == {
            "id": 4, "long_name": "Taxes", "description": "Taxes paid",
        }
        # Base movements can then be added under it
        store.add_base_movement("Income tax", "loss", "Income tax", result_type="Tax")
        assert store.load().base_movements["Income tax"].result_type.name == "Tax"

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (("Op", "Operative", "d"), "already exists"),
            (("", "X", "d"), "name is required"),
            (("yes", "X", "d"), "needs quoting"),
            (("X", " ", "d"), "long name is required"),
            (("X", "X", ""), "description is required"),
        ],
    )
    def test_add_result_type_rejects_invalid(self, settings, args, message):
        before = settings.base_movements_path.read_text()
        with pytest.raises(ValueError, match=message):
            LedgerStore(settings).add_result_type(*args)
        assert settings.base_movements_path.read_text() == before

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (("Food", "loss", "d"), "already exists"),
            (("X", "nope", "d"), "Invalid movement type"),
            (("X", "loss", ""), "description is required"),
            (("X", "transfer", "d", "Ca", "Ca"), "must be different"),
            (("X", "transfer", "d", "Eq"), "non-selectable account"),
            (("X", "gain", "d", "Nope", "", "Op"), "non-selectable account"),
            (("X", "gain", "d"), "require a result type"),
            (("X", "loss", "d", "", "", "Nope"), "Unknown result type"),
        ],
    )
    def test_add_base_movement_rejects_invalid(self, settings, args, message):
        before = settings.base_movements_path.read_text()
        with pytest.raises(ValueError, match=message):
            LedgerStore(settings).add_base_movement(*args)
        assert settings.base_movements_path.read_text() == before


def card_labels(html: str) -> list[str]:
    return [c.split("</span>")[0].split(" <small>")[0] for c in html.split('<span class="label">')[1:]]


class TestPages:
    def test_index(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "New movement" in r.text
        assert "$1,000.50" in r.text
        assert "Balances" not in r.text and "New account" not in r.text
        assert card_labels(r.text) == []  # no gains, losses and profit cards

    def test_accounts(self, client):
        r = client.get("/accounts")
        assert r.status_code == 200
        assert 'href="/accounts" aria-current="page"' in r.text
        assert card_labels(r.text) == ["Assets", "Liabilities", "Equity"]
        cards = r.text.split('<div class="cards">')[1].split("</div>\n    </div>")[0]
        assert cards.count("$1,060.75") == 2  # assets (901.00 + 159.75) and equity
        assert "$0.00" in cards  # no liabilities
        assert "Balances" in r.text and "New account" in r.text
        assert "<td class=\"muted\">Bank account</td>" in r.text  # descriptions
        new_account = r.text.split('hx-post="/accounts"')[1].split("</form>")[0]
        assert 'value="equity"' not in new_account
        assert 'value="asset"' in new_account
        for field in ('name="currency"', 'name="is_liquid"', 'name="initial_amount"', 'name="initial_date"'):
            assert field in new_account
        assert '<option value="ARS" title="Pesos" selected>ARS (base)</option>' in new_account
        assert 'name="initial_amount" inputmode="decimal" value="0.00" placeholder="0.00" required' in new_account
        assert "<th>Currency</th><th>Liquid</th>" in r.text
        assert "<td>USD</td>" in r.text
        assert '<option value="net_positive" >Net positive</option>' in new_account
        assert "<th>Special</th><th>Checks</th>" in r.text

    def test_accounts_form_adds_an_account_with_a_check(self, client, settings):
        form = {"name": "Fi", "acc_type": "asset", "description": "Fictitious", "initial_amount": "0", "check": "net_zero"}
        r = client.post("/accounts", data=form, headers=HTMX)
        assert "Added account Fi" in r.text
        fi_row = r.text.split("<strong>Fi</strong>")[1].split("</tr>")[0]
        assert "<td >Net zero</td>" in fi_row
        assert '<td class="muted">-</td>' in r.text.split("<strong>Ca</strong>")[1].split("</tr>")[0]

        r = client.post("/accounts", data=form | {"name": "Fi2", "initial_amount": "5"}, headers=HTMX)
        assert "breaks its net_zero check" in r.text
        assert '<option value="net_zero" selected>Net zero</option>' in r.text  # kept on error

    def test_api_accounts_checks(self, client):
        r = client.post("/api/accounts", json={
            "name": "Sv", "type": "asset", "description": "Savings", "initial_amount": 0, "checks": ["net_positive"],
        })
        assert r.status_code == 201
        sv = next(a for a in client.get("/api/accounts").json() if a["name"] == "Sv")
        assert sv["checks"] == ["net_positive"]
        r = client.post("/api/accounts", json={
            "name": "Sv2", "type": "asset", "description": "Savings", "initial_amount": 0, "checks": ["nope"],
        })
        assert r.status_code == 422

    def test_balances_grouped_by_currency_or_liquidity(self, client):
        assert 'name="accounts-group"' not in client.get("/results").text  # only in Accounts
        r = client.get("/accounts")
        assert 'name="accounts-group" value="currency" checked' in r.text
        assert 'name="accounts-group" value="liquidity"' in r.text
        by_currency = r.text.split('data-group="currency"')[1].split('data-group="liquidity"')[0]
        by_liquidity = r.text.split('data-group="liquidity"')[1]
        assert '<span class="bar-name">ARS</span>' in by_currency
        assert '<span class="bar-name">USD</span>' in by_currency
        assert '<span class="bar-name">Liquid</span>' in by_liquidity
        assert '<span class="bar-name">Illiquid</span>' in by_liquidity  # Cc

    def test_accounts_partial_for_month(self, client):
        r = client.get("/accounts", params={"month": "2026-01"}, headers=HTMX)
        assert "<html" not in r.text and "as of end of 2026-01" in r.text

    def test_month_is_shared_between_tabs(self, client, settings):
        with settings.ledger_path.open("a") as f:
            f.write("2026-02-01;Food;Eq;100;Ca;\n")
        client.get("/results", params={"month": "2026-02"})
        for path in ["/", "/accounts", "/results"]:
            assert '<option value="2026-02" selected>' in client.get(path).text, path
        # An explicit "All time" is remembered too
        client.get("/accounts", params={"month": ""})
        assert '<option value="2026-02" selected>' not in client.get("/").text
        # Months without movements, or malformed ones, are ignored
        client.cookies.set("pledger_month", "2030-01")
        assert "as of end of" not in client.get("/accounts").text
        assert client.get("/", params={"month": "<x>"}).status_code == 200

    def test_visibility_toggle_marks_the_amounts(self, client):
        for path in ["/", "/accounts", "/results", "/investments", "/debt"]:
            r = client.get(path)
            assert 'id="visibility"' in r.text and 'class="eye-closed"' in r.text, path
        r = client.get("/")
        assert '<td class="num money" data-sort="10.0">$10.00</td>' in r.text
        assert 'data-confirm-money="$10.00"' in r.text  # masked in the delete question too
        assert 'class="total money"' in client.get("/results").text

    def test_currency_setting(self, settings):
        client = TestClient(create_app(replace(settings, currency="ARS")))
        assert "ARS 1,000.50" in client.get("/").text

    def test_index_partial_for_htmx(self, client):
        r = client.get("/", params={"month": "2026-01", "account": "Ca"}, headers=HTMX)
        assert r.status_code == 200
        assert "<html" not in r.text
        assert "Salary" not in r.text.split("<tbody>")[1]  # Salary doesn't touch Ca

    def test_form_picks_type_then_base_movement(self, client):
        r = client.get("/")
        form = r.text.split('id="movement-form"')[1].split("</form>")[0]
        types = form.split('class="segmented types"')[1].split("</div>")[0]
        assert re.findall(r'<label class="(\w+)">', types) == ["gain", "loss", "transfer"]
        assert re.findall(r'value="(\w+)" checked', types) == ["loss"]  # a loss by default
        assert types.count('type="radio" name="move_type"') == 3
        assert 'hx-get="/form/bases" hx-target="#base-fields"' in types
        base_select = form.split('name="base_movement"')[1].split("</select>")[0]
        assert "Food" in base_select and "Gift" not in base_select  # loss only

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
        assert "Added Gift: $5.00" in r.text and "(dated in the future)" not in r.text
        assert len(rows(settings)) == 5

    def test_future_date_warns_but_is_saved(self, client, settings):
        future = (dt.date.today() + dt.timedelta(days=3)).isoformat()
        form = {"move_date": future, "base_movement": "Gift", "acc_debit": "Ca", "acc_credit": "Eq", "amount": "5"}
        r = client.post("/movements", data=form, headers=HTMX)
        assert "Added Gift: $5.00 (dated in the future)" in r.text
        assert len(rows(settings)) == 5
        # The form warns on the client before submitting
        assert "data-future-warning" in r.text and "This date is in the future" in r.text

    @pytest.mark.parametrize(("amount", "message"), [("-5", "cannot be negative"), ("nan", "Invalid amount")])
    def test_negative_amount_is_blocked(self, client, settings, amount, message):
        form = {"move_date": "2026-02-01", "base_movement": "Gift", "acc_debit": "Ca", "acc_credit": "Eq", "amount": amount}
        r = client.post("/movements", data=form, headers=HTMX)
        assert 'class="msg error"' in r.text and message in r.text
        assert len(rows(settings)) == 4
        body = {**form, "amount": -5}
        assert client.post("/api/movements", json=body).status_code == 422
        assert len(rows(settings)) == 4

    def test_static_urls_are_versioned(self, client):
        assert "style.css?v=" in client.get("/").text

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

    def test_result_types_and_base_movements(self, client):
        r = client.get("/results")
        assert r.status_code == 200
        assert "Withdraw" in r.text
        assert "New account" not in r.text and "New base movement" in r.text
        # Only credit: descriptions are required and the default loss type fixes debit
        assert r.text.count('<em class="optional">(optional)</em>') == 1
        assert r.text.count('name="description" value="" required') == 2
        assert "New result type" in r.text and ">Operative</td>" in r.text
        # Free sides read "-" (any account)
        assert ">Any<" not in r.text and ">any<" not in r.text
        assert '<option value="" title="Any account">-</option>' in r.text
        assert '<span class="muted" title="Any account">-</span>' in r.text

    def test_create_result_type(self, client, settings):
        data = {"name": "Tax", "long_name": "Taxes", "description": "Taxes paid"}
        r = client.post("/results/result-types", data=data, headers=HTMX)
        assert "Added result type Tax" in r.text and ">Taxes</td>" in r.text
        r = client.post("/results/result-types", data=data, headers=HTMX)
        assert 'class="msg error"' in r.text and "already exists" in r.text

    def test_help(self, client):
        r = client.get("/help")
        assert r.status_code == 200
        assert 'href="/help" aria-current="page"' in r.text
        assert "help-logo" in r.text and "> Pledger</h1>" in r.text
        # Section headings carry the same numbers as the index
        toc = r.text.split('<ol>')[1].split("</ol>")[0]
        titles = [li.split("</span> ")[1].split("</a>")[0] for li in toc.split("<li>")[1:]]
        assert len(titles) == 8 and titles[-1] == "Source code and license"
        about = r.text.split('id="about"')[1].split("</section>")[0]
        assert 'href="https://github.com/trOOnies/pledger"' in about and "GNU General Public License v3.0" in about
        for n, title in enumerate(titles, start=1):
            assert f'<span class="help-num">{n}.</span>' in r.text
            assert f"</span> {title}</h2>" in r.text

    @pytest.mark.parametrize(
        "path", ["/", "/accounts", "/investments", "/debt", "/results", "/help"]
    )
    def test_footer(self, client, path):
        text = client.get(path).text
        assert "&copy; 2026 Pledger by trOOnies" in text
        assert "Made with Claude Code" in text

    def test_tables_are_sortable(self, client):
        index, setup = client.get("/").text, client.get("/results").text
        accounts = client.get("/accounts").text
        assert 'data-sortable="movements"' in index
        assert 'data-sortable="balances-asset"' in accounts
        assert 'data-sort="1000.5"' in index  # sort by raw amount, not formatted text
        assert 'data-sortable="setup-result-types"' in setup
        assert 'data-sortable="setup-bases-gain"' in setup

    def test_create_account(self, client, settings):
        r = client.post(
            "/accounts",
            data={"name": "Sv", "acc_type": "asset", "description": "Savings", "initial_amount": "0.00"},
            headers=HTMX,
        )
        assert "Added account Sv" in r.text
        assert "<strong>Sv</strong>" in r.text
        assert "Sv" in LedgerStore(settings).load().accounts

        data = {"name": "Sv", "acc_type": "asset", "description": "x", "initial_amount": "0"}
        r = client.post("/accounts", data=data, headers=HTMX)
        assert 'class="msg error"' in r.text and "already exists" in r.text

        data = {"name": "Sv2", "acc_type": "asset", "description": "", "initial_amount": "0"}
        r = client.post("/accounts", data=data, headers=HTMX)
        assert "description is required" in r.text

        # The initial amount must always be sent
        data = {"name": "Sv2", "acc_type": "asset", "description": "d"}
        for extra in ({}, {"initial_amount": ""}):
            r = client.post("/accounts", data={**data, **extra}, headers=HTMX)
            assert "initial amount is required" in r.text
        assert "Sv2" not in LedgerStore(settings).load().accounts

        data = {
            "name": "Sv3", "acc_type": "asset", "description": "d", "currency": "USD",
            "is_liquid": "true", "initial_amount": "1,000.00", "initial_date": "2026-01-02",
        }
        assert "Added account Sv3" in client.post("/accounts", data=data, headers=HTMX).text
        sv3 = LedgerStore(settings).load().accounts["Sv3"]
        assert (sv3.currency.name, sv3.is_liquid, sv3.initial_amount) == ("USD", True, 1000.0)
        assert sv3.initial_date == dt.date(2026, 1, 2)
        # A failed submit keeps what was typed
        data = {**data, "name": "Sv4", "initial_amount": "-5"}
        r = client.post("/accounts", data=data, headers=HTMX)
        assert "cannot be negative" in r.text
        assert 'value="-5"' in r.text and 'value="2026-01-02"' in r.text
        assert 'name="is_liquid" value="true" checked' in r.text

    def test_create_base_movement(self, client, settings):
        data = {
            "name": "Rent", "move_type": "loss", "description": "Rent", "credit": "Bk",
            "result_type": "Gifts",
        }
        r = client.post("/results/base-movements", data=data, headers=HTMX)
        assert "Added base movement Rent" in r.text
        assert LedgerStore(settings).load().base_movements["Rent"].result_type.name == "Gifts"
        r = client.get("/form/bases", params={"move_type": "loss"})
        assert "Rent" in r.text

    def test_form_base_sides(self, client):
        r = client.get("/form/base-sides", params={"move_type": "gain"})
        assert '<select name="debit"' in r.text
        assert 'name="credit"' not in r.text and 'value="Eq"' in r.text
        assert '<select name="result_type"' in r.text and ">Operative</option>" in r.text
        r = client.get("/form/base-sides", params={"move_type": "transfer"})
        assert 'name="result_type"' not in r.text

    def test_broken_ledger_shows_error_page(self, client, settings):
        settings.ledger_path.write_text(
            "move_date;base_movement;acc_debit;amount;acc_credit\n2026-01-01;Nope;Ca;1;Eq\n"
        )
        r = client.get("/")
        assert r.status_code == 500
        assert "Could not load the ledger" in r.text

    def test_results(self, client):
        r = client.get("/results")
        assert r.status_code == 200
        assert 'href="/results" aria-current="page"' in r.text
        assert "Equity over time" in r.text and "Assets &amp; liabilities" not in r.text
        assert "Salary" in r.text and "Food" in r.text
        assert "by result type" in r.text and "Result by type" in r.text
        assert "$1,060.75" in r.text  # final equity
        assert 'data-sortable="equity"' in r.text

    def test_debt_over_equity_is_orange(self, client):
        r = client.get("/accounts")
        debt = r.text.split("<h2>Debt over equity")[1].split("</section>")[0]
        liquidity = r.text.split("<h2>Net liquidity over equity")[1].split("</section>")[0]
        assert "--line: var(--series-2)" in debt and "--line: var(--series-1)" in liquidity
        assert r.text.index("<h2>Net liquidity over equity") < r.text.index("<h2>Debt over equity")

    def test_new_account_type_buttons(self, client):
        form = client.get("/accounts").text.split('hx-post="/accounts"')[1].split("</form>")[0]
        types = form.split('class="segmented types"')[1].split("</div>")[0]
        assert re.findall(r'<label class="(\w+)">', types) == ["asset", "liability"]
        assert re.findall(r'name="acc_type" value="(\w+)" checked', types) == ["asset"]
        assert "hx-get" not in types  # nothing to load
        r = client.post("/accounts", data={
            "name": "Lo", "acc_type": "liability", "description": "Loan", "initial_amount": "0",
        })
        assert "Added account Lo" in r.text
        types = r.text.split('class="segmented types"')[1].split("</div>")[0]
        assert re.findall(r'name="acc_type" value="(\w+)" checked', types) == ["liability"]  # kept

    def test_currencies_equity_in_other_currencies(self, client):
        r = client.get("/currencies")
        assert "Equity in other currencies" in card_labels(r.text)
        card = r.text.split("Equity in other currencies</span>")[1].split("</div>")[0]
        assert "84.9%" in card and "$901.00" in card  # Bk, in USD, of the equity of 1,060.75
        assert build_currencies(LedgerStore(client.app.state.store.settings).load())["other_equity_share"] == (
            pytest.approx(901 / 1060.75)
        )

    def test_account_charts(self, client):
        r = client.get("/accounts")
        assert "Assets &amp; liabilities <small>today</small>" in r.text
        assert "$901.00" in r.text and "$159.75" in r.text  # Bk and Ca balances

    def test_results_partial_for_month(self, client, settings):
        with settings.ledger_path.open("a") as f:
            f.write("2026-02-01;Food;Eq;100;Ca;\n")
        r = client.get("/results", params={"month": "2026-02"}, headers=HTMX)
        assert "<html" not in r.text and "New result type" not in r.text
        assert "in 2026-02" in r.text
        assert "Salary" in r.text  # no gains in February, but still listed at zero
        assert "-$100.00" in r.text and "$960.75" in r.text

    def test_equity_cards(self, client, settings):
        r = client.get("/results")
        assert card_labels(r.text) == ["Equity change", "Equity change", "Equity in months of expenses"]
        # From the initial 100.50 of Bk to 1,060.75; 50.25 of food in the 5 days from 2025-12-31 to 2026-01-04
        assert "+955.47%" in r.text and "3.5 months" in r.text
        metrics = build_results(LedgerStore(settings).load())
        assert metrics["equity_change"] == pytest.approx(1060.75 / 100.5 - 1)
        assert metrics["monthly_expenses"] == pytest.approx(50.25 / (5 / (365.25 / 12)))
        assert metrics["months_covered"] == pytest.approx(1060.75 / metrics["monthly_expenses"])
        # The real change is smaller: prices went up between 2025-12-30 and 2026-01-04
        assert 0 < metrics["equity_real_change"] < metrics["equity_change"]

    def test_equity_cards_in_a_month(self, settings):
        with settings.ledger_path.open("a") as f:
            f.write("2026-02-01;Food;Eq;106.075;Ca;\n")
        snapshot = LedgerStore(settings).load()
        metrics = build_results(snapshot, "2026-02")
        assert metrics["equity_change"] == pytest.approx(-0.1)  # from 1,060.75 to 954.675
        assert metrics["equity_real_change"] is None  # no CPI after 2026-01
        # 156.325 of food from 2025-12-31 to 2026-02-28, but the months covered are at the end of February
        assert metrics["months_covered"] == pytest.approx(954.675 / (156.325 / (60 / (365.25 / 12))))
        empty = build_results(snapshot, "2025-11")
        assert empty["equity_change"] is None and empty["months_covered"] is None


class TestCharts:
    def test_composition_folds_the_smallest_into_other(self):
        rows = composition({"a": 50, "b": 30, "c": 10, "d": 6, "e": 4, "zero": 0}, limit=3)
        assert [r["name"] for r in rows] == ["a", "b", "Other (3)"]
        assert rows[-1]["value"] == 20 and rows[-1]["share"] == 20
        assert [r["name"] for r in rows[-1]["rest"]] == ["c", "d", "e"]
        assert rows[0]["width"] == 100 and rows[1]["width"] == 60

    def test_composition_ordered_keeps_order_and_everything(self):
        rows = composition({"b": 0, "a": 50, "c": -5, "d": 10}, limit=2, ordered=True)
        assert [r["name"] for r in rows] == ["b", "a", "c", "d"]
        assert "rest" not in rows[-1]
        assert rows[2]["width"] == 0 and rows[1]["width"] == 100
        assert composition({"a": 0}, ordered=True)[0]["share"] == 0

    def test_charts_keep_defined_order(self, settings):
        with settings.ledger_path.open("a") as f:
            f.write("2026-01-05;Card fee;Eq;500;Cc;\n")  # makes Cc a liability, Card fee the top loss
        snapshot = LedgerStore(settings).load()
        groupings = build_accounts(snapshot)["groupings"]
        assets = groupings["liquidity"]["assets"][0]["children"]
        assert [r["name"] for r in assets] == ["Ca", "Bk"]  # accounts.yaml order
        assert [r["name"] for r in groupings["currency"]["assets"]] == ["ARS", "USD"]
        assert [r["name"] for r in groupings["liquidity"]["liabilities"]] == ["Illiquid"]
        charts = build_results(snapshot)
        losses = charts["losses"][0]["children"]
        assert [r["name"] for r in losses] == ["Food", "Card fee"]  # definition order, not size
        gains = charts["gains"]
        assert [r["name"] for r in gains] == ["Operative", "Gifts"]  # result type id order, no Initial
        gains = build_results(LedgerStore(settings).load(), initial=True)["gains"]
        assert [r["name"] for r in gains] == ["Operative", "Gifts", "Initial"]

    def test_charts_list_everything_even_at_zero(self, settings):
        with settings.ledger_path.open("a") as f:
            f.write("2026-02-01;Food;Eq;100;Ca;\n")
        snapshot = LedgerStore(settings).load()
        charts = build_results(snapshot, "2026-02")
        # Only Food moved in February; every other bar is still there, at zero
        by_currency = build_accounts(snapshot, "2026-02")["groupings"]["currency"]
        assert [c["name"] for r in by_currency["assets"] for c in r["children"]] == ["Ca", "Bk"]
        assert [c["name"] for r in by_currency["liabilities"] for c in r["children"]] == ["Cc"]
        gains = charts["gains"]
        assert [(r["name"], r["value"]) for r in gains] == [("Operative", 0), ("Gifts", 0)]
        assert [c["name"] for c in gains[0]["children"]] == ["Salary"]
        assert [c["name"] for c in gains[1]["children"]] == ["Gift"]
        losses = charts["losses"]
        assert [r["name"] for r in losses] == ["Operative", "Gifts"]
        assert [(c["name"], c["value"]) for c in losses[0]["children"]] == [("Food", 100), ("Card fee", 0)]
        assert losses[1]["children"] == []  # Gifts has no loss base movements

    def test_gains_and_losses_by_result_type(self, settings):
        charts = build_results(LedgerStore(settings).load())
        gains = {r["name"]: r for r in charts["gains"]}
        assert set(gains) == {"Operative", "Gifts"}
        assert gains["Operative"]["value"] == pytest.approx(1000.5)
        assert [c["name"] for c in gains["Operative"]["children"]] == ["Salary"]
        # Children are drawn on the parent's scale
        gift = gains["Gifts"]["children"][0]
        assert gift["share"] == pytest.approx(gains["Gifts"]["share"])
        assert gift["width"] == pytest.approx(gains["Gifts"]["width"])
        assert [r["name"] for r in charts["losses"]] == ["Operative", "Gifts"]  # at zero

        statement = charts["statement"]
        assert [r["name"] for r in statement] == ["Operative", "Gifts"]  # id order
        assert statement[0]["gains"] == pytest.approx(1000.5)
        assert statement[0]["losses"] == pytest.approx(-50.25)
        assert statement[0]["net"] == pytest.approx(950.25)
        assert statement[1]["cumulative"] == pytest.approx(960.25)

    def test_result_by_type_waterfall(self, settings, client):
        chart = build_results(LedgerStore(settings).load())["waterfall"]
        # Gains then losses of each result type, in id order, then the profit from 0
        assert [(c["short"], c["kind"], c["start"], c["end"]) for c in chart["columns"]] == [
            ("Op", "gain", 0, 1000.5), ("Op", "loss", 1000.5, 950.25),
            ("Gifts", "gain", 950.25, 960.25), ("Gifts", "loss", 960.25, 960.25),
            ("Profit", "total", 0, 960.25),
        ]
        assert [t["label"] for t in chart["x_ticks"]] == ["Op", "Gifts", "Profit"]
        assert chart["x_ticks"][0]["x"] == pytest.approx(20)  # between its two columns
        assert build_results(LedgerStore(settings).load(), "2025-12")["waterfall"] is None
        r = client.get("/results")
        section = r.text.split("<h2>Result by type")[1].split("</section>")[0]
        assert 'class="line-chart columns waterfall"' in section
        assert section.index("<summary>Show table</summary>") < section.index('<table class="statement">')
        section = client.get("/results", params={"month": "2025-12"}).text.split("<h2>Result by type")[1]
        assert "waterfall" not in section.split("</section>")[0] and "<summary>" not in section.split("</section>")[0]

    def test_nice_ticks(self):
        assert nice_ticks(51.9e6, 75.8e6) == [50e6, 60e6, 70e6, 80e6]
        assert nice_ticks(0, 0.9) == [0, 0.25, 0.5, 0.75, 1.0]

    def test_equity_series(self, settings):
        snapshot = LedgerStore(settings).load()
        series = equity_series(snapshot)
        # Bk's initial 100.50 is in the equity, but not a change of its own
        assert [(p["date"], p["value"], p["change"]) for p in series] == [
            ("2026-01-01", 1101.0, 1000.5), ("2026-01-03", 1050.75, -50.25),
            ("2026-01-04", 1060.75, 10),
        ]
        assert next(p["date"] for p in equity_series(snapshot, initial=True)) == "2025-12-31"
        chart = build_results(snapshot)["equity"]
        assert chart["points"][0]["x"] == 0 and chart["points"][-1]["x"] == 100
        assert chart["change"] == pytest.approx(960.25)  # the profit
        # The real equity, at the prices of its last date
        assert chart["real_date"] == "2026-01-04" and chart["real_path"]
        first, *_, last = chart["points"]
        assert last["real"] == pytest.approx(last["value"]) and first["real"] > first["value"]
        assert build_results(snapshot, "2025-12")["equity"] is None
        assert build_results(snapshot, "2025-12", initial=True)["equity"]["change"] == pytest.approx(100.5)

    def test_month_end_charts(self, settings):
        with settings.ledger_path.open("a") as f:
            f.write("2026-03-01;Card fee;Eq;60.75;Cc;\n")  # no movements in February
        snapshot = LedgerStore(settings).load()
        charts = build_accounts(snapshot)
        # Liabilities over equity: none until Cc's fee
        assert [(p["month"], p["value"]) for p in charts["debt_series"]] == [
            ("2025-12", 0), ("2026-01", 0), ("2026-02", 0), ("2026-03", pytest.approx(60.75 / 1000)),
        ]
        # Liquid assets (Bk, Ca) minus liquid liabilities (none: Cc isn't liquid)
        assert charts["liquidity_series"][-1]["value"] == pytest.approx(1060.75 / 1000)
        assert charts["debt_ratio"]["y_ticks"][-1]["label"] == "8%"
        # Up to the month picked
        assert [p["month"] for p in build_accounts(snapshot, "2026-01")["debt_series"]] == ["2025-12", "2026-01"]

    def test_ratios_without_positive_equity(self, settings):
        LedgerStore(settings).add("2026-02-01", "Card fee", "Eq", "Cc", "2000")
        charts = build_accounts(LedgerStore(settings).load())
        assert charts["debt_series"][-1]["value"] is None
        assert "M" in charts["debt_ratio"]["path"] and charts["debt_ratio"]["last"]["month"] == "2026-01"

    def test_month_end_charts_render(self, client):
        r = client.get("/accounts")
        for title in ("Debt over equity", "Net liquidity over equity"):
            assert f"<h2>{title} <small>all time, " in r.text
        # The currency shares are in the Currencies tab
        assert "Currencies over time" not in r.text and 'class="line-chart stacked"' not in r.text


class TestCurrencies:
    TODAY = dt.date(2026, 3, 10)  # March hasn't ended: projected

    @pytest.fixture
    def values(self, revalued):
        settings = revalued
        folder = settings.currencies_path.parent
        (folder / "base_currency.csv").write_text("month;monthly_inflation\n2026-01;10%\n2026-02;10%\n2026-03;5%\n")
        (folder / "currencies.csv").write_text(
            "date;USD\n2026-01-31;1000\n2026-02-14;\n2026-02-28;1210\n2027-01-01;2000\n"
        )
        return settings

    def test_build_currencies(self, values):
        currencies = build_currencies(LedgerStore(values).load(), self.TODAY)
        assert currencies["base"] == {
            "name": "ARS", "description": "Pesos", "accounts": 2,
            "assets": pytest.approx(159.75), "liabilities": pytest.approx(0), "own": [],
        }
        # Each month's inflation, the CPI at its end; March is projected
        assert [(m["month"], m["value"], m["cpi"], m["projected"]) for m in currencies["inflation_months"]] == [
            ("2026-01", 0.1, pytest.approx(110), False),
            ("2026-02", 0.1, pytest.approx(121), False),
            ("2026-03", 0.05, pytest.approx(127.05), True),
        ]
        assert currencies["last_month"]["month"] == "2026-02"
        assert currencies["last_month"]["last_12"] is None and currencies["last_12_monthly"] is None
        assert [c["projected"] for c in currencies["inflation_chart"]["columns"]] == [False, False, True]
        # The daily CPI: known up to the end of February, projected after it
        cpi, projected = currencies["cpi_chart"]["lines"]
        assert (cpi["name"], cpi["dashed"], projected["name"], projected["dashed"]) == ("CPI", False, "Projected", True)
        assert (cpi["series"][0]["date"], cpi["series"][-1]["date"]) == ("2025-12-31", "2026-02-28")
        assert (projected["series"][0]["date"], projected["series"][-1]["date"]) == ("2026-02-28", "2026-03-31")
        assert cpi["series"][-1]["value"] == pytest.approx(121) and len(cpi["series"]) == 60  # every day
        (usd,) = currencies["others"]
        # Bk holds 0.901 USD: revalued to 1,090.21 on 2026-02-28 and 1,802 on 2027-01-01
        assert (usd["name"], usd["slot"], usd["accounts"], usd["assets"]) == ("USD", 2, 1, pytest.approx(1802))
        assert usd["own"] == [{
            "name": "Bk", "description": "Bank account", "type": "asset",
            "balance": pytest.approx(0.901), "base_balance": pytest.approx(1802), "result": pytest.approx(901),
        }]
        assert [p["value"] for p in usd["series"]] == [1000, 1210, 2000]  # without the empty cell
        # Deflated by the daily CPI, in ARS of its last date with a CPI: 1000 at 110 is 1100 at 121
        assert usd["real_date"] == "2026-02-28"
        assert [p["real"] for p in usd["series"]] == [pytest.approx(1100), pytest.approx(1210), None]
        assert usd["real_growth"]["total"] == pytest.approx(0.1)
        # Nominal and real on one chart, the tooltip listing the lines with a value
        assert [line["name"] for line in usd["chart"]["lines"]] == ["Nominal", "Real"]
        assert usd["chart"]["points"][0]["rows"] == [["Nominal", "1,000.00"], ["Real", "1,100.00"]]
        assert usd["chart"]["points"][-1]["rows"] == [["Nominal", "2,000.00"]]

    def test_inflation_of_the_last_12_months(self, values):
        rows = "".join(f"2025-{m:02};1%\n" for m in range(1, 13)) + "2026-01;2%\n"
        (values.currencies_path.parent / "base_currency.csv").write_text("month;monthly_inflation\n" + rows)
        currencies = build_currencies(LedgerStore(values).load(), dt.date(2026, 2, 1))
        months = currencies["inflation_months"]
        assert months[10]["last_12"] is None and months[11]["last_12"] == pytest.approx(1.01 ** 12 - 1)
        assert currencies["last_month"]["last_12"] == pytest.approx(1.01 ** 11 * 1.02 - 1)
        assert currencies["last_12_monthly"] == pytest.approx((1.01 ** 11 * 1.02) ** (1 / 12) - 1)

    def test_long_daily_series_are_sampled(self, values):
        rows = "".join(f"{y}-{m:02};1%\n" for y in range(2016, 2026) for m in range(1, 13))
        (values.currencies_path.parent / "base_currency.csv").write_text("month;monthly_inflation\n" + rows)
        currencies = build_currencies(LedgerStore(values).load(), self.TODAY)
        (cpi,) = currencies["cpi_chart"]["lines"]  # every month has ended
        assert len(cpi["series"]) <= 400 and cpi["series"][-1]["date"] == "2025-12-31"
        assert [t["label"] for t in currencies["cpi_chart"]["x_ticks"]][:2] == ["2016", "2018"]  # by year

    def test_currency_shares(self, settings):
        with settings.ledger_path.open("a") as f:
            f.write("2026-03-01;Card fee;Eq;60.75;Cc;\n")
        shares = build_currencies(LedgerStore(settings).load())["currency_shares"]
        assert [a["name"] for a in shares["areas"]] == ["ARS", "USD"]  # base first, at the bottom
        assert [p["rows"] for p in shares["points"]][:2] == [
            [["USD", "100.0%"], ["ARS", "0.0%"]], [["USD", "84.9%"], ["ARS", "15.1%"]],
        ]
        assert shares["areas"][1]["last"] == pytest.approx(901 / 1060.75)

    def test_currencies_page(self, client, values):
        r = client.get("/currencies")
        assert r.status_code == 200
        assert '<a href="/currencies" aria-current="page">Currencies</a>' in r.text
        titles = ("Customer price index", "Monthly inflation", "USD in ARS", "Currencies over time")
        positions = [r.text.index(f"<h2>{title} <small>") for title in titles]
        assert positions == sorted(positions)  # in this order, the currency shares at the bottom
        assert 'class="line dashed"' in r.text and "10.00%" in r.text  # the monthly inflation
        assert 'class="line-chart stacked"' in r.text and 'data-sortable="currencies"' in r.text
        assert 'data-sortable="inflation-monthly"' in r.text

    def test_column_chart_with_negative_values(self):
        chart = column_chart([{"month": "2026-01", "value": 0.02}, {"month": "2026-02", "value": -0.01}], percent)
        up, down = chart["columns"]
        zero = next(t for t in chart["y_ticks"] if t["axis"])
        assert zero["label"] == "0%" and not up["negative"] and down["negative"]
        assert up["top"] + up["height"] == pytest.approx(zero["y"]) and down["top"] == zero["y"]

    def test_currencies_page_without_values(self, client, settings):
        folder = settings.currencies_path.parent
        (folder / "currencies.csv").write_text("date;USD\n")
        (folder / "base_currency.csv").write_text("month;monthly_inflation\n")
        r = client.get("/currencies")
        assert r.status_code == 200
        assert "No months in <code>base_currency.csv</code> yet." in r.text
        assert "No values of USD in <code>currencies.csv</code> yet." in r.text


def with_conversion_bases(settings: Settings) -> Settings:
    """The settings, with the transfers of the conversions of USD, at the end of the `transfer` section."""
    path = settings.base_movements_path
    path.write_text(path.read_text() + "  USD - Bought: Pesos to dollars\n  USD - Sold: Dollars to pesos\n")
    return settings


class TestConversions:
    HEADER = "move_date;base_movement;acc_debit;amount_base_currency;acc_credit;comment;amount_other_currency\n"

    @pytest.fixture
    def conv(self, revalued, tmp_path) -> Settings:
        """With a conversion: 1 USD bought for 1,100 ARS on 2026-01-05, and a USD value after it."""
        with_conversion_bases(revalued)
        path = tmp_path / "movements" / "conversion.csv"
        path.parent.mkdir()
        path.write_text(self.HEADER + "2026-01-05;Move;Bk;1,100.00;Ca;Bought;1.0000\n", encoding="utf-8")
        (tmp_path / "currencies.csv").write_text("date;USD\n2026-01-01;1000\n2026-01-10;1200\n")
        return replace(revalued, conversions_path=path)

    @pytest.fixture
    def conv_client(self, conv) -> TestClient:
        return TestClient(create_app(conv))

    @staticmethod
    def rows(df) -> list[tuple]:
        return list(df[["move_date", "base_movement", "acc_debit", "amount", "acc_credit", "comment"]].itertuples())

    def test_are_in_the_ledger_in_the_base_currency(self, conv):
        snapshot = LedgerStore(conv).load()
        assert len(snapshot.rows) == 4  # the ledger CSV alone
        assert self.rows(snapshot.conversion_rows) == [(-2, "2026-01-05", "Move", "Bk", "1,100.00", "Ca", "Bought")]
        dates = list(snapshot.all_rows.move_date)
        assert dates == sorted(dates)  # in date order
        balances = snapshot.ledger().balances()
        assert balances["Ca"] == pytest.approx(159.75 - 1100)

    def test_accounts_in_other_currencies_are_revalued(self, conv):
        # Bk: 0.1005 USD initially (at the first value), 1.0005 of salary, 0.2 withdrawn and 1 bought:
        # 1.901 USD, 2,001 ARS in the ledger, worth 2,281.20 at 1,200
        snapshot = LedgerStore(conv).load()
        assert self.rows(snapshot.result_rows) == [
            (-3, "2026-01-10", "USD - Result (G)", "Bk", "280.20", "Eq", "1.9010 USD at 1,200.0000"),
        ]
        assert snapshot.ledger().balances()["Bk"] == pytest.approx(2281.2)
        assert snapshot.other_balances == {"Bk": pytest.approx(1.901)}

    def test_a_lower_value_is_a_loss(self, conv, tmp_path):
        (tmp_path / "currencies.csv").write_text("date;USD\n2026-01-01;1000\n2026-01-10;900\n2026-01-20;900\n")
        snapshot = LedgerStore(conv).load()
        assert self.rows(snapshot.result_rows) == [  # nothing more on 2026-01-20
            (-3, "2026-01-10", "USD - Result (L)", "Eq", "290.10", "Bk", "1.9010 USD at 900.0000"),
        ]
        assert snapshot.ledger().balances()["Bk"] == pytest.approx(1710.9)

    def test_the_result_base_movements_are_required_to_revalue(self, conv):
        conv.base_movements_path.write_text(conv.base_movements_path.read_text().replace(RESULT_YAML, ""))
        with pytest.raises(LedgerDataError, match=re.escape("'USD - Result (G)' is required to revalue")):
            LedgerStore(conv).load()

    def test_results_cannot_be_in_the_ledger_csv(self, conv):
        with conv.ledger_path.open("a") as f:
            f.write("2026-01-05;USD - Result (G);Bk;5;Eq;\n")
        with pytest.raises(LedgerDataError, match=re.escape("cannot have movements of ['USD - Result (G)']")):
            LedgerStore(conv).load()

    @pytest.mark.parametrize(
        ("row", "error"),
        [
            ("2026-01-05;Move;Cc;100;Ca;;1", "between an account in ARS and one in another currency, not Cc and Ca"),
            ("2026-01-05;Gift;Bk;100;Eq;;1", "must be of a non-internal transfer, not 'Gift'"),
            ("2026-01-05;Nope;Bk;100;Ca;;1", "Unknown base movement of a conversion: 'Nope'"),
            ("2026-13-05;Move;Bk;100;Ca;;1", "Invalid date of a conversion"),
            ("2026-01-05;Move;Bk;100;Ca;;x", "Invalid amount in USD of a conversion"),
            ("2026-01-05;Move;Bk;100;Ca;;0", "must both be zero or both positive"),
        ],
    )
    def test_invalid_rows(self, conv, row, error):
        conv.conversions_path.write_text(self.HEADER + row + "\n")
        with pytest.raises(LedgerDataError, match=re.escape(error)):
            LedgerStore(conv).load()

    def test_missing_columns(self, conv):
        conv.conversions_path.write_text("move_date;base_movement;acc_debit;acc_credit;amount_base_currency\n")
        with pytest.raises(LedgerDataError, match=re.escape("missing the columns ['amount_other_currency']")):
            LedgerStore(conv).load()

    def test_ids_of_the_generated_movements(self, conv):
        snapshot = LedgerStore(conv).load()
        ids = snapshot.ids
        assert [ids[ix] for ix in snapshot.initial_rows.index] == ["A-Bk"]
        assert [ids[ix] for ix in snapshot.conversion_rows.index] == [None]  # not frozen yet
        assert all(ids[ix].startswith("R-Bk-") for ix in snapshot.result_rows.index)
        assert len(set(ids.values()) - {None}) == len(ids) - 5  # all but the 4 ledger rows and the conversion
        LedgerStore(conv).freeze_ids()
        ids = LedgerStore(conv).load().ids
        assert {ids[ix] for ix in snapshot.conversion_rows.index} == {"C-1"} and len(set(ids.values())) == len(ids)

    def test_conversions_table_shows_their_ids(self, conv, conv_client):
        LedgerStore(conv).freeze_ids()
        currencies = conv_client.get("/currencies").text
        assert movement_ids(currencies.split('data-sortable="conversions"')[1]) == ["C-1"]
        ledger = conv_client.get("/", params={"initial": True}).text
        assert "C-1" in movement_ids(ledger)
        # The circles link to the same row, or to the row they are generated from
        assert 'href="/currencies#movement-C-1"' in ledger and '<tr id="movement-C-1">' in currencies
        assert 'href="/currencies#account-Bk"' in ledger and '<tr id="account-Bk">' in currencies
        assert 'href="/accounts#account-Bk"' in ledger and '<tr id="account-Bk">' in conv_client.get("/accounts").text

    def test_add_and_delete(self, conv):
        store = LedgerStore(conv)
        store.add_conversion("2026-01-06", "USD - Sold", "Ca", "Bk", "1234.5", "1.2", "Sold; half")
        assert conv.conversions_path.read_text().splitlines()[-1] == (
            "2026-01-06;USD - Sold;Ca;1,234.50;Bk;Sold, half;1.2000;C-2"
        )
        conversions = store.load().conversions
        assert len(conversions) == 2
        with pytest.raises(StaleRowError):
            store.delete_conversion(1, "2026-01-06|USD - Sold|1,234.50|9.0000")
        store.delete_conversion(1, conversion_fingerprint(conversions.iloc[1]))
        assert conv.conversions_path.read_text() == (
            self.HEADER.replace("\n", ";id\n") + "2026-01-05;Move;Bk;1,100.00;Ca;Bought;1.0000;C-1\n"
        )

    def test_edit(self, conv, conv_client):
        LedgerStore(conv).freeze_ids()
        fp = conversion_fingerprint(LedgerStore(conv).load().conversions.iloc[0])
        assert 'hx-get="/conversions/0/edit"' in conv_client.get("/currencies").text
        r = conv_client.get("/conversions/0/edit", params={"fingerprint": fp})
        assert "Edit conversion" in r.text and 'hx-post="/conversions/0/edit"' in r.text
        assert 'value="1,100.00"' in r.text and 'value="1.0000"' in r.text
        r = conv_client.post("/conversions/0/edit", data={
            "fingerprint": fp, "move_date": "2026-01-05", "sold": "ARS", "bought": "USD", "acc_from": "Ca",
            "acc_to": "Bk", "amount_sold": "1200", "amount_bought": "1", "comment": "Bought",
        })
        assert "Updated C-1 USD - Bought: 1,200.00 ARS sold for 1.00 USD" in r.text
        assert r.headers["HX-Trigger"] == "conversions-changed"
        assert conv.conversions_path.read_text().splitlines()[-1] == "2026-01-05;USD - Bought;Bk;1,200.00;Ca;Bought;1.0000;C-1"

    def test_adding_creates_the_csv(self, revalued, tmp_path):
        path = tmp_path / "movements" / "conversion.csv"
        store = LedgerStore(replace(with_conversion_bases(revalued), conversions_path=path))
        store.add_conversion("2026-01-06", "USD - Bought", "Bk", "Ca", "100", "0.1")
        assert path.read_text() == (
            self.HEADER.replace("\n", ";id\n") + "2026-01-06;USD - Bought;Bk;100.00;Ca;;0.1000;C-1\n"
        )

    @pytest.mark.parametrize(
        ("args", "error"),
        [
            (("2026-01-06", "USD - Bought", "Ca", "Cc", "100", "1"), "between an account in ARS and one in another currency"),
            (("2026-01-06", "USD - Bought", "Bk", "Bk", "100", "1"), "must be different"),
            (("2026-01-06", "USD - Bought", "Bk", "Ca", "0", "0"), "must be positive"),
            (("2026-01-06", "USD - Bought", "Bk", "Ca", "-1", "1"), "cannot be negative"),
            (("2026-01-06", "Move", "Bk", "Ca", "100", "1"), "must be of 'USD - Bought' or 'USD - Sold', not 'Move'"),
            (("2026-01-06", "USD - Sold", "Bk", "Ca", "100", "1"), "into Bk is a 'USD - Bought' conversion"),
        ],
    )
    def test_add_rejects_invalid_ones(self, conv, args, error):
        before = conv.conversions_path.read_text()
        with pytest.raises(ValueError, match=re.escape(error)):
            LedgerStore(conv).add_conversion(*args)
        assert conv.conversions_path.read_text() == before

    def test_without_a_conversions_csv(self, revalued):
        assert len(LedgerStore(revalued).load().conversion_rows) == 0
        with pytest.raises(ValueError, match="No conversions CSV is set"):
            LedgerStore(revalued).add_conversion("2026-01-06", "Move", "Bk", "Ca", "100", "0.1")

    def test_add_exchange_picks_the_base_movement_from_the_currencies(self, conv):
        store = LedgerStore(conv)
        assert store.add_exchange("2026-01-06", "USD", "ARS", "Bk", "Ca", "0.5", "600") == "USD - Sold"
        assert conv.conversions_path.read_text().splitlines()[-1] == "2026-01-06;USD - Sold;Ca;600.00;Bk;;0.5000;C-2"
        for args, error in [
            (("USD", "USD", "Bk", "Bk"), "sell or buy ARS for another currency, not USD for USD"),
            (("ARS", "EUR", "Ca", "Bk"), "Unknown currency: 'EUR'"),
            (("ARS", "USD", "Bk", "Ca"), "The account from must be in ARS, but Bk is in USD"),
        ]:
            with pytest.raises(ValueError, match=re.escape(error)):
                store.add_exchange("2026-01-06", *args, "1", "1")

    def test_add_exchange_needs_the_base_movement(self, revalued, tmp_path):
        store = LedgerStore(replace(revalued, conversions_path=tmp_path / "conversion.csv"))
        with pytest.raises(ValueError, match=re.escape("No 'USD - Bought' base movement: add it as a transfer")):
            store.add_exchange("2026-01-06", "ARS", "USD", "Ca", "Bk", "1000", "1")

    def test_conversion_fields_follow_the_currencies(self, conv_client):
        def fields(**params):
            r = conv_client.get("/form/conversion-fields", params=params, headers=HTMX)
            sold = r.text.split('name="sold"')[1].split("</select>")[0]
            bought = r.text.split('name="bought"')[1].split("</select>")[0]
            return re.search(r"<option selected>(\w+)", sold)[1], re.search(r"<option selected>(\w+)", bought)[1], r.text

        assert fields()[:2] == ("ARS", "USD")  # those of the last conversion
        sold, bought, text = fields(sold="USD", bought="USD", changed="sold")
        assert (sold, bought) == ("USD", "ARS") and "USD - Sold" in text
        assert '>Bk</option>' in text.split('name="acc_from"')[1].split("</select>")[0]
        assert fields(sold="ARS", bought="ARS", changed="bought")[:2] == ("USD", "ARS")

    def test_not_in_the_ledger_tab(self, conv, conv_client):
        form = conv_client.get("/").text.split('id="movement-form"')[1].split("</form>")[0]
        assert "USD - Bought" not in form and "USD - Sold" not in form
        with pytest.raises(ValueError, match="are added from the Currencies tab"):
            LedgerStore(conv).add("2026-01-06", "USD - Bought", "Bk", "Ca", "100")

    def test_ledger_tab_links_them_to_the_currencies_tab(self, conv_client):
        r = conv_client.get("/")
        assert '<a class="source" href="/currencies" title="Added, edited and deleted from the Currencies tab"' in r.text
        assert "🔵</a>" in r.text
        assert r.text.count('hx-post="/movements/') == 4  # only the CSV rows
        assert "USD - Result (G)" in r.text  # internal: not deletable either
        (moved,) = [m for m in conv_client.get("/api/movements").json() if m["conversion"]]
        assert (moved["date"], moved["amount"]) == ("2026-01-05", 1100.0)

    def test_ids_link_between_the_ledger_and_currencies_tabs(self, conv, conv_client):
        LedgerStore(conv).freeze_ids()
        ledger = conv_client.get("/").text
        assert '<a class="id-link" href="/currencies#movement-C-1"' in ledger and 'id="movement-C-1"' in ledger
        currencies = conv_client.get("/currencies").text
        assert '<a class="id-link" href="/?month=2026-01#movement-C-1"' in currencies
        assert 'id="movement-C-1"' in currencies

    def test_currencies_tab_lists_adds_and_deletes_them(self, conv, conv_client):
        r = conv_client.get("/currencies")
        assert "<h2>Accounts in other currencies <small>" in r.text and "1.9010" in r.text
        assert 'hx-post="/conversions"' in r.text and 'hx-post="/conversions/0/delete"' in r.text
        form = r.text.split('id="conversion-form"')[1].split("</form>")[0]
        assert 'name="base_movement"' not in form  # it follows from the currencies
        assert '<select name="sold"' in form and '<select name="bought"' in form
        r = conv_client.post("/conversions", data={
            "move_date": "2026-01-06", "sold": "ARS", "bought": "USD", "acc_from": "Ca", "acc_to": "Bk",
            "amount_sold": "1,000", "amount_bought": "1",
        })
        assert "Added USD - Bought: 1,000.00 ARS sold for 1.00 USD" in r.text
        assert r.headers["HX-Trigger"] == "conversions-changed"
        assert "2026-01-06;USD - Bought;Bk;1,000.00;Ca;;1.0000" in conv.conversions_path.read_text()
        r = conv_client.post("/conversions", data={
            "move_date": "2026-01-06", "sold": "ARS", "bought": "USD", "acc_from": "Ca", "acc_to": "Cc",
            "amount_sold": "1", "amount_bought": "1",
        })
        assert 'class="msg error"' in r.text and "The account to must be in USD, but Cc is in ARS" in r.text
        listed = conv_client.get("/conversions").text
        assert listed.count('hx-post="/conversions/') == 2
        first, _ = conv_client.get("/api/conversions").json()  # newest first
        assert (first["date"], first["rate"], first["bought"]) == ("2026-01-06", 1000.0, True)
        r = conv_client.post(f"/conversions/{first['index']}/delete", data={"fingerprint": first["fingerprint"]})
        assert r.status_code == 200
        assert conv_client.post("/conversions/0/delete", data={"fingerprint": "x"}).status_code == 409
        assert len(conv_client.get("/api/conversions").json()) == 1

    def test_api(self, conv_client):
        body = {"move_date": "2026-01-06", "base_movement": "USD - Sold", "acc_debit": "Ca", "acc_credit": "Bk",
                "amount_base": 500, "amount_other": 0.5}
        assert conv_client.post("/api/conversions", json=body).status_code == 201
        assert conv_client.post("/api/conversions", json={**body, "acc_credit": "Cc"}).status_code == 422
        (first, _) = conv_client.get("/api/conversions").json()
        assert conv_client.delete(f"/api/conversions/{first['index']}", params={"fingerprint": first["fingerprint"]}).status_code == 200


class TestApi:
    def test_summary(self, client):
        data = client.get("/api/summary").json()
        assert data["profit"] == pytest.approx(960.25)  # without the initial movements
        assert client.get("/api/summary", params={"initial": True}).json()["profit"] == pytest.approx(1060.75)
        assert data["balances"]["Bk"] == pytest.approx(901.0)
        assert data["assets"] == pytest.approx(1060.75)
        assert data["liabilities"] == pytest.approx(0)
        assert data["equity"] == pytest.approx(1060.75)

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
        accounts = {a["name"]: a for a in client.get("/api/accounts").json()}
        assert len(accounts) == 4
        assert accounts["Bk"] == {
            "name": "Bk", "description": "Bank account", "type": "asset", "currency": "USD",
            "is_liquid": True, "initial_amount": 100.5, "initial_date": "2025-12-31", "checks": [],
        }
        assert accounts["Cc"]["initial_date"] is None
        bases = {b["name"]: b for b in client.get("/api/base-movements").json()}
        assert bases["Withdraw"]["debit"] == "Ca"
        assert bases["Withdraw"]["result_type"] is None
        assert bases["Gift"]["result_type"] == "Gifts"

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
        # The initial amount is required, and can't be null
        body = {"name": "Sv", "type": "asset", "description": "Savings"}
        assert client.post("/api/accounts", json=body).status_code == 422
        assert client.post("/api/accounts", json={**body, "initial_amount": None}).status_code == 422
        body["initial_amount"] = 0.0
        assert client.post("/api/accounts", json=body).status_code == 201
        assert client.post("/api/accounts", json=body).status_code == 422
        usd = {
            "name": "Us", "type": "asset", "description": "d", "initial_amount": 50,
            "currency": "USD", "is_liquid": True,
        }
        assert client.post("/api/accounts", json=usd).status_code == 201
        assert client.post("/api/accounts", json={**usd, "name": "Eu", "currency": "EUR"}).status_code == 422
        accounts = {a["name"]: a for a in client.get("/api/accounts").json()}
        assert accounts["Us"]["currency"] == "USD" and accounts["Us"]["is_liquid"]
        assert accounts["Us"]["initial_amount"] == 50.0
        equity = {"name": "Eq2", "type": "equity", "description": "x"}
        assert client.post("/api/accounts", json=equity).status_code == 422
        assert client.post("/api/accounts", json={**equity, "initial_amount": 0}).status_code == 422
        no_description = {"name": "Sv2", "type": "asset", "initial_amount": 0}
        assert client.post("/api/accounts", json=no_description).status_code == 422
        body = {"name": "Deposit", "type": "transfer", "description": "d", "debit": "Sv", "credit": "Ca"}
        assert client.post("/api/base-movements", json=body).status_code == 201
        bases = {b["name"]: b for b in client.get("/api/base-movements").json()}
        assert bases["Deposit"]["debit"] == "Sv"
        body = {"name": "Tip", "type": "loss", "description": "d", "result_type": "Op"}
        assert client.post("/api/base-movements", json=body).status_code == 201
        body = {"name": "Tip2", "type": "loss", "description": "d"}
        assert client.post("/api/base-movements", json=body).status_code == 422

    def test_result_types(self, client):
        assert [rt["name"] for rt in client.get("/api/result-types").json()] == ["Op", "Gifts", "Init"]
        body = {"name": "Tax", "long_name": "Taxes", "description": "d"}
        assert client.post("/api/result-types", json=body).status_code == 201
        assert client.post("/api/result-types", json=body).status_code == 422
        result_types = client.get("/api/result-types").json()
        assert result_types[-1] == {
            "id": 4, "name": "Tax", "long_name": "Taxes", "description": "d", "internal": False,
        }


class TestInvestments:
    def test_investment_accounts_are_loaded(self, settings, tmp_path):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace("liability:\n", "  Br:\n    description: Broker\nliability:\n"), encoding="utf-8"
        )
        path = tmp_path / "investment.yaml"
        path.write_text("Br:\n  T:\n    type: stock\n    amount: 10\n    quantity: 1\n", encoding="utf-8")
        accounts = LedgerStore(replace(settings, investments_path=path)).load().accounts
        assert accounts["Br"].investments["T"]["total_quantity"] == 1.0
        assert accounts["Br"].initial_amount == 10.0


class TestInternalResultTypes:
    @staticmethod
    def initial_rows(settings) -> list[tuple]:
        df = LedgerStore(settings).load().initial_rows
        return list(df[["move_date", "base_movement", "acc_debit", "amount", "acc_credit"]].itertuples())

    def test_are_generated_from_the_accounts_yaml(self, settings):
        # Bk sets 100.5 on 2025-12-31; Ca and Cc start at zero
        assert self.initial_rows(settings) == [(-1, "2025-12-31", "Initial (G)", "Bk", "100.5", "Eq")]
        snapshot = LedgerStore(settings).load()
        assert len(snapshot.rows) == 4 and snapshot.ledger().balances()["Bk"] == 901.0
        assert snapshot.ledger(snapshot.rows).balances()["Bk"] == 800.5  # the CSV alone

    def test_new_accounts_get_one_without_touching_the_csv(self, settings):
        before = settings.ledger_path.read_text()
        store = LedgerStore(settings)
        store.add_account("Sv", "asset", "Savings", "1,234.5", "USD", False, "2026-01-15")
        store.add_account("Lo", "liability", "Loan", 300)
        store.add_account("Zr", "asset", "Zero", 0)
        assert settings.ledger_path.read_text() == before
        assert self.initial_rows(settings)[1:] == [
            (-2, "2026-01-15", "Initial (G)", "Sv", "1234.5", "Eq"),
            (-3, "2026-01-01", "Initial (L)", "Eq", "300.0", "Lo"),  # on the first ledger date
        ]
        balances = store.load().ledger().balances()
        assert (balances["Sv"], balances["Lo"]) == (1234.5, 300.0)

    def test_without_ledger_rows_start_on_the_first_initial_date(self, settings):
        settings.ledger_path.write_text("move_date;base_movement;acc_debit;amount;acc_credit;comment\n")
        LedgerStore(settings).add_account("Lo", "liability", "Loan", 300)
        assert [r[1] for r in self.initial_rows(settings)] == ["2025-12-31", "2025-12-31"]

    def test_a_negative_debt_is_a_gain(self, settings, tmp_path):
        text = settings.accounts_path.read_text()
        settings.accounts_path.write_text(text.replace("equity:\n", "  Db:\n    description: Debts\nequity:\n"))
        path = tmp_path / "creditors.yaml"
        path.write_text("Db:\n  people:\n    Ann: 50\n    Bob: -80\n")
        settings = replace(settings, creditors_path=path)
        assert self.initial_rows(settings)[1:] == [(-2, "2026-01-01", "Initial (G)", "Db", "30.0", "Eq")]

    def test_are_hidden_by_default(self, client):
        assert all(m["base"] != "Initial (G)" for m in client.get("/api/movements").json())
        r = client.get("/")
        assert "Initial (G)" not in r.text and 'name="initial" value="true" >' in r.text
        assert "Initial (G)" not in client.get("/results", headers=HTMX).text  # the charts

    def test_can_be_shown_but_not_deleted(self, client):
        movements = client.get("/api/movements", params={"initial": True}).json()
        assert movements[-1]["base"] == "Initial (G)"  # the oldest
        r = client.get("/", params={"initial": True})
        assert 'name="initial" value="true" checked>' in r.text
        assert r.text.count('hx-post="/movements/') == 4  # only the CSV rows
        assert "$100.50" in r.text  # Bk's initial movement
        assert ">Initial<" in client.get("/results", params={"initial": True}).text

    def test_cannot_be_added_by_hand(self, settings):
        with pytest.raises(ValueError, match=re.escape("'Initial (G)' is internal")):
            LedgerStore(settings).add("2026-01-05", "Initial (G)", "Ca", "Eq", "5")

    def test_cannot_be_in_the_ledger_csv(self, settings, client):
        with settings.ledger_path.open("a") as f:
            f.write("2026-01-05;Initial (G);Ca;5;Eq;\n")
        with pytest.raises(LedgerDataError, match=re.escape("cannot have movements of ['Initial (G)']")):
            LedgerStore(settings).load()
        assert client.get("/").status_code == 500

    @pytest.mark.parametrize(
        ("old", "new", "missing"),
        [
            ("        Initial (G): Initial balance\n", "", "Initial (G)"),
            ("        Initial (L): Initial balance\n", "", "Initial (L)"),
            ("      gain:\n        Initial (G)", "      loss:\n        Initial (G)", "Initial (G)"),  # a loss
            ("        Initial (G): Initial balance\n",
             "        Initial (G):\n          description: d\n          debit: Ca\n", "Initial (G)"),
        ],
    )
    def test_are_required_to_load(self, settings, client, old, new, missing):
        path = settings.base_movements_path
        assert old in path.read_text()
        path.write_text(path.read_text().replace(old, new, 1))
        with pytest.raises(LedgerDataError, match=re.escape(f"base movement {missing!r} is required")):
            LedgerStore(settings).load()
        r = client.get("/")
        assert r.status_code == 500 and f"{missing!r} is required" in r.text.replace("&#39;", "'")

    def test_cannot_get_new_base_movements(self, settings, client):
        with pytest.raises(ValueError, match="'Init' is internal"):
            LedgerStore(settings).add_base_movement("X", "gain", "d", result_type="Init")
        r = client.get("/form/base-sides", params={"move_type": "gain"})
        assert ">Operative<" in r.text and ">Initial<" not in r.text

    def test_share_ids_with_the_other_result_types(self, settings, client):
        LedgerStore(settings).add_result_type("New", "New one", "d")
        result_types = client.get("/api/result-types").json()
        assert [(rt["id"], rt["name"], rt["internal"]) for rt in result_types] == [
            (1, "Op", False), (2, "Gifts", False), (3, "Init", True), (4, "New", False),
        ]


class TestInvestmentSides:
    @pytest.fixture
    def settings(self, settings, tmp_path):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace("liability:\n", "  Br:\n    description: Broker\nliability:\n"), encoding="utf-8"
        )
        path = tmp_path / "investment.yaml"
        path.write_text("Br:\n  T: stock\n", encoding="utf-8")
        with settings.base_movements_path.open("a") as f:
            f.write("  Buy:\n    description: Buy investments\n    debit: INVESTMENT\n")
        return replace(settings, investments_path=path)

    @pytest.fixture
    def client(self, settings):
        return TestClient(create_app(settings))

    def test_add_movement(self, settings, client):
        store = LedgerStore(settings)
        with pytest.raises(ValueError, match="Debit account must be an investment account: Bk is not"):
            store.add("2026-02-01", "Buy", "Bk", "Ca", "1")
        store.add("2026-02-01", "Buy", "Br", "Ca", "1")
        assert rows(settings).iloc[-1]["acc_debit"] == "Br"

    def test_form_offers_only_investment_accounts(self, client):
        r = client.get("/form/accounts", params={"base_movement": "Buy"})
        debit, credit = r.text.split('name="acc_credit"')
        assert "(investment account)" in debit and "<option >Br</option>" in debit
        assert "<option >Bk</option>" not in debit and "<option >Bk</option>" in credit

    def test_add_base_movement(self, settings, client):
        LedgerStore(settings).add_base_movement("Sell", "transfer", "d", credit=INVESTMENT)
        LedgerStore(settings).add_base_movement("Swap", "transfer", "d", INVESTMENT, INVESTMENT)
        bases = {b["name"]: b for b in client.get("/api/base-movements").json()}
        assert (bases["Buy"]["debit"], bases["Buy"]["credit"]) == ("INVESTMENT", None)
        assert (bases["Sell"]["debit"], bases["Sell"]["credit"]) == (None, "INVESTMENT")
        assert (bases["Swap"]["debit"], bases["Swap"]["credit"]) == ("INVESTMENT", "INVESTMENT")
        r = client.get("/form/base-sides", params={"move_type": "transfer"})
        assert '<option value="INVESTMENT" >Any investment account</option>' in r.text
        assert 'title="Any investment account">Investment<' in client.get("/results").text

    @pytest.mark.parametrize("name", [INVESTMENT, DEBT])
    def test_is_not_an_account_name(self, settings, name):
        with pytest.raises(ValueError, match=f"'{name}' is reserved"):
            LedgerStore(settings).add_account(name, "asset", "d", "0")


class TestDebtSides:
    @pytest.fixture
    def settings(self, settings, tmp_path):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace("equity:\n", "  De:\n    description: Debts\nequity:\n"), encoding="utf-8"
        )
        path = tmp_path / "creditors.yaml"
        path.write_text("De:\n  people:\n    Dad: 0\n", encoding="utf-8")
        path_yaml = settings.base_movements_path
        path_yaml.write_text(path_yaml.read_text().replace(INITIAL_YAML, INITIAL_YAML + DEBT_YAML, 1))
        return replace(settings, creditors_path=path)

    @pytest.fixture
    def client(self, settings):
        return TestClient(create_app(settings))

    def test_add_movement(self, settings):
        store = LedgerStore(settings)
        with pytest.raises(ValueError, match="Credit account must be a debt account: Cc is not"):
            store.add("2026-02-01", "Debt - Incurred", "Ca", "Cc", "1", debt=True)
        store.add("2026-02-01", "Debt - Incurred", "Ca", "De", "1", debt=True)
        assert rows(settings).iloc[-1]["acc_credit"] == "De"

    def test_not_in_the_ledger_tab(self, settings, client):
        with pytest.raises(ValueError, match="'Debt - Incurred' are added from the Debt tab"):
            LedgerStore(settings).add("2026-02-01", "Debt - Incurred", "Ca", "De", "1")
        with pytest.raises(ValueError, match="'Move' is not a debt base movement"):
            LedgerStore(settings).add("2026-02-01", "Move", "Ca", "De", "1", debt=True)
        assert client.post("/api/movements", json={
            "move_date": "2026-02-01", "base_movement": "Debt - Incurred", "acc_debit": "Ca", "acc_credit": "De", "amount": 1,
        }).status_code == 422
        r = client.get("/form/bases", params={"move_type": "transfer"})
        assert "Debt" not in r.text and "Loan" not in r.text and "Move" in r.text

    def test_add_debt_movement_names_who_first(self, settings):
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "10", "Dad", "for the car")
        store.add_debt_movement("2026-02-02", "Debt - Incurred", "Ca", "De", "5", "Dad", "dad's lunch")  # already named
        store.add_debt_movement("2026-02-03", "Debt - Incurred", "Ca", "De", "1", "Dad")
        assert list(rows(settings).comment[-3:]) == ["Dad: for the car", "dad's lunch", "Dad"]
        with pytest.raises(ValueError, match="'Mum' is not someone of the debt account De"):
            store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "1", "Mum")
        with pytest.raises(ValueError, match="Pick who"):
            store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "1", " ")
        debts = {c.name: c.amount for a in store.load().debt_accounts() for c in a.creditors.values()}
        assert debts == {"Dad": pytest.approx(16)}

    def test_no_debt_movements_of_companies(self, settings, tmp_path):
        settings.creditors_path.write_text("De:\n  people:\n    Dad: 0\n  companies:\n    Acme: 10\n")
        store = LedgerStore(settings)
        with pytest.raises(ValueError, match="'Acme' is a company: its debt comes from the salaries of the Work tab"):
            store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "1", "Acme")
        r = TestClient(create_app(settings)).get("/debt")
        form = r.text.split('id="debt-form"')[1].split("</form>")[0]
        assert "Acme" not in form and '<optgroup label="Companies">' not in form and "Dad" in form
        assert "<strong>Acme</strong>" in r.text  # still listed with its debt

    def test_debt_tab_form(self, settings, client):
        r = client.get("/debt")
        form = r.text.split('id="debt-form"')[1].split("</form>")[0]
        # The four debt base movements, as two pairs of buttons
        assert '<span class="field-label">Debt</span>' in form and '<span class="field-label">Loan</span>' in form
        assert 'value="Debt - Incurred" checked' in form and form.count('name="base_movement"') == 4
        for label in ("Incurred", "Paid", "Given", "Collected"):
            assert f"{label}\n      </label>" in form
        assert '<optgroup label="People">' in form
        assert 'hx-trigger="debt-changed from:body"' in r.text
        r = client.post("/debt/movements", data={
            "move_date": "2026-02-01", "base_movement": "Debt - Incurred", "acc_debit": "Ca", "acc_credit": "De",
            "amount": "20", "who": "Dad", "comment": "Groceries",
        })
        assert "Added Debt - Incurred (Dad): $20.00" in r.text and r.headers["HX-Trigger"] == "debt-changed"
        assert rows(settings).iloc[-1]["comment"] == "Dad: Groceries"
        r = client.post("/debt/movements", data={
            "move_date": "2026-02-01", "base_movement": "Debt - Incurred", "acc_debit": "Ca", "acc_credit": "De",
            "amount": "20", "who": "",
        })
        assert 'class="msg error"' in r.text and "Pick who" in r.text
        assert client.post("/api/debt/movements", json={
            "move_date": "2026-02-01", "base_movement": "Debt - Incurred", "acc_debit": "Ca", "acc_credit": "De",
            "amount": 1, "who": "Dad",
        }).status_code == 201

    def test_edit_debt_movement(self, settings, client):
        LedgerStore(settings).add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "20", "Dad", "Groceries")
        assert 'hx-get="/debt/movements/4/edit"' in client.get("/debt", params={"month": ""}).text
        fp = row_fingerprint(rows(settings).iloc[4])
        r = client.get("/debt/movements/4/edit", params={"fingerprint": fp})
        assert "Edit debt movement" in r.text and 'hx-post="/debt/movements/4/edit"' in r.text
        assert "<option selected>Dad</option>" in r.text and 'name="comment" value="Groceries"' in r.text
        r = client.post("/debt/movements/4/edit", data={
            "fingerprint": fp, "move_date": "2026-02-02", "base_movement": "Debt - Incurred", "acc_debit": "Ca",
            "acc_credit": "De", "amount": "25", "who": "Dad", "comment": "Dinner",
        })
        assert "Updated L-5 Debt - Incurred (Dad): $25.00" in r.text and r.headers["HX-Trigger"] == "debt-changed"
        assert "New debt movement" in r.text
        assert rows(settings).iloc[4][["move_date", "amount", "comment"]].tolist() == ["2026-02-02", "25.00", "Dad: Dinner"]
        # Only the debt movements are edited from the Debt tab, and they aren't from the Ledger one
        with pytest.raises(ValueError, match="'Food' is not a debt base movement"):
            LedgerStore(settings).edit(2, None, "2026-01-03", "Food", "Eq", "Ca", "1", debt=True)
        with pytest.raises(ValueError, match="'Debt - Incurred' are edited from the Debt tab"):
            LedgerStore(settings).edit(4, None, "2026-02-02", "Debt - Incurred", "Ca", "De", "1", "Dad")

    def test_delete_debt_movement(self, settings, client):
        LedgerStore(settings).add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "20", "Dad")
        assert 'hx-post="/debt/movements/4/delete"' in client.get("/debt", params={"month": ""}).text
        fp = row_fingerprint(rows(settings).iloc[4])
        # Not from the Ledger tab, which links to the Debt tab instead
        assert client.post("/movements/4/delete", data={"fingerprint": fp}).status_code == 422
        assert client.delete("/api/movements/4").status_code == 422
        r = client.post("/debt/movements/4/delete", data={"fingerprint": fp})
        assert r.status_code == 200 and r.headers["HX-Trigger"] == "debt-changed"
        assert len(rows(settings)) == 4
        LedgerStore(settings).add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "20", "Dad")
        assert client.delete("/api/debt/movements/4").status_code == 200 and len(rows(settings)) == 4

    def test_who_filter(self, settings, client):
        settings.creditors_path.write_text("De:\n  people:\n    Dad: 0\n    Mum: 0\n", encoding="utf-8")
        store = LedgerStore(settings)
        store.add_debt_movement("2026-02-01", "Debt - Incurred", "Ca", "De", "20", "Dad")
        store.add_debt_movement("2026-02-02", "Loan - Given", "De", "Ca", "5", "Mum")
        with settings.ledger_path.open("a") as f:
            f.write("2026-02-03;Debt - Paid;De;1;Ca;no one\n")
        data = build_debts(store.load(), "", "Mum")
        assert [m["who"] for m in data["movements"]] == ["Mum"] and data["who"] == "Mum"
        assert [m["comment"] for m in build_debts(store.load(), "", "-")["movements"]] == ["no one"]
        assert len(build_debts(store.load(), "", "Nobody")["movements"]) == 3  # unknown: everyone
        r = client.get("/debt", params={"month": "", "who": "Dad"})
        assert '<form id="who-filter"' in r.text and "<option selected>Dad</option>" in r.text
        assert "<h2>Movements <small>1, all time</small></h2>" in r.text

    def test_debt_tab_form_without_debt_base_movements(self, settings, client):
        path = settings.base_movements_path
        path.write_text(path.read_text().replace("    Loan - Collected:\n      description: d\n      credit: DEBT\n", ""))
        form = client.get("/debt").text.split('id="debt-form"')[1].split("</form>")[0]
        assert "The Debt tab needs these base movements" in form and "&#39;Loan - Collected&#39;" in form
        assert form.count('name="base_movement"') == 3

    def test_internal_ones_link_to_their_row_in_the_debt_tab(self, settings, client):
        with settings.ledger_path.open("a") as f:
            f.write("2026-02-03;Debt - Paid;De;5;Ca;\n")
        LedgerStore(settings).freeze_ids()
        # To the Debt tab in the month of the movement, so it isn't filtered out
        assert 'href="/debt?month=2026-02#movement-L-5" title="Added, edited and deleted from the Debt tab"' in client.get("/").text
        assert '<tr id="movement-L-5">' in client.get("/debt", params={"month": "2026-02"}).text

    def test_form_offers_only_debt_accounts(self, client):
        r = client.get("/form/accounts", params={"base_movement": "Debt - Incurred"})
        debit, credit = r.text.split("<span>Credit")
        assert "(debt account)" in credit and "<option >De</option>" in credit
        assert "<option >Cc</option>" not in credit and "<option >Cc</option>" in debit

    def test_add_base_movement(self, settings, client):
        LedgerStore(settings).add_base_movement("Repay", "transfer", "d", debit=DEBT)
        bases = {b["name"]: b for b in client.get("/api/base-movements").json()}
        assert (bases["Debt - Incurred"]["debit"], bases["Debt - Incurred"]["credit"]) == (None, "DEBT")
        assert (bases["Repay"]["debit"], bases["Repay"]["credit"]) == ("DEBT", None)
        r = client.get("/form/base-sides", params={"move_type": "transfer"})
        assert '<option value="DEBT" >Any debt account</option>' in r.text
        assert 'title="Any debt account">Debt<' in client.get("/results").text

    def test_accounts_special_column(self, settings, tmp_path):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace("liability:\n", "  Br:\n    description: Broker\nliability:\n"), encoding="utf-8"
        )
        path = tmp_path / "investment.yaml"
        path.write_text("Br:\n  T: stock\n", encoding="utf-8")
        r = TestClient(create_app(replace(settings, investments_path=path))).get("/accounts")
        assert "<th>Liquid</th><th>Special</th>" in r.text
        specials = {
            row.split("<strong>")[1].split("</strong>")[0]: row.split("</td>")[4].split(">")[-1]
            for row in re.split(r'<tr id="account-[^"]+">', r.text.split("<tbody>", 1)[1])[1:]
            if "<strong>" in row
        }
        assert specials == {"Ca": "-", "Bk": "-", "Br": "Investment", "Cc": "-", "De": "Debt", "Eq": "-"}


class TestDebtAccounts:
    def test_debt_accounts_are_loaded(self, settings, tmp_path):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace("    description: Credit card\n    initial_amount: 0\n", "    description: Credit card\n"),
            encoding="utf-8",
        )
        path = tmp_path / "creditors.yaml"
        path.write_text("Cc:\n  people:\n    Dad: 10\n", encoding="utf-8")
        accounts = LedgerStore(replace(settings, creditors_path=path)).load().accounts
        assert accounts["Cc"].creditors["Dad"].is_creditor
        assert accounts["Cc"].initial_amount == 10.0


class TestInvestmentsPage:
    @pytest.fixture
    def settings(self, settings, tmp_path):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace(
                "liability:\n",
                "  Br:\n    description: Broker\n  Br2:\n    description: Other broker\nliability:\n",
            ),
            encoding="utf-8",
        )
        path = tmp_path / "investment.yaml"
        path.write_text(
            "Br:\n  T:\n    type: stock\n    amount: 300\n    quantity: 1.5\n"
            "  AL30:\n    type: public_bond\n    amount: 100\n    quantity: 1000\n"
            "Br2:\n  T:\n    type: stock\n    amount: 100\n    quantity: 0.25\n"
            "  QQQ: etf\n",
            encoding="utf-8",
        )
        return replace(settings, investments_path=path)

    @pytest.fixture
    def client(self, settings):
        return TestClient(create_app(settings))

    def test_investments(self, client):
        r = client.get("/investments")
        assert r.status_code == 200
        assert 'href="/investments" aria-current="page"' in r.text
        assert card_labels(r.text) == ["Invested"]  # no interest on money defined
        assert '<span class="num money ">$500.00</span>' in r.text
        assert 'data-sortable="investments"' in r.text
        held, closed = r.text.split("<h2>No longer held <small>1</small></h2>")
        assert "<td>Public bond</td>" in held and "<td>ETF</td>" not in held
        assert "<strong>QQQ</strong></td>\n        <td>ETF</td>" in closed  # nothing held
        assert ">1,000</td>" in r.text and ">1.5</td>" in r.text and ">0.25</td>" in r.text
        assert ">60.0%</td>" in r.text  # T in Br: 300 of 500

    def test_price_is_amount_over_quantity(self, client):
        r = client.get("/investments")
        assert '<th class="num" title="Amount over quantity">Price</th>' in r.text
        assert 'data-sort="200.0">$200.00</td>' in r.text  # T in Br: 300 for 1.5
        assert 'data-sort="0.1">$0.10</td>' in r.text  # AL30: 100 for 1,000

    def test_investments_grouped_by_type_or_account(self, client):
        r = client.get("/investments")
        by_type = r.text.split('data-group="type"')[1].split('data-group="account"')[0]
        by_account = r.text.split('data-group="account"')[1]
        # Types in their defined order; names qualified by account, as they repeat
        assert by_type.index('<span class="bar-name">Public bond</span>') < by_type.index(
            '<span class="bar-name">Stock</span>'
        )
        assert '<span class="bar-name">T (Br2)</span>' in by_type
        assert '<span class="bar-name">Br2</span>' in by_account
        assert '<span class="bar-name">AL30</span>' in by_account


class TestDebtPage:
    @pytest.fixture
    def settings(self, settings, tmp_path):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace("    description: Credit card\n    initial_amount: 0\n", "    description: Credit card\n"),
            encoding="utf-8",
        )
        path = tmp_path / "creditors.yaml"
        path.write_text(
            "Cc:\n  people:\n    Dad: 100\n    Ana: -30\n    Leo: 0\n  services:\n    Bank: 50.5\n  companies:\n",
            encoding="utf-8",
        )
        return replace(settings, creditors_path=path)

    @pytest.fixture
    def client(self, settings):
        return TestClient(create_app(settings))

    def test_debt(self, client):
        r = client.get("/debt")
        assert r.status_code == 200
        assert 'href="/debt" aria-current="page"' in r.text
        assert card_labels(r.text) == ["We owe", "Owed to us", "Net debt"]
        cards = r.text.split('<div class="cards">')[1].split("</div>\n  </div>")[0]
        assert "$150.50" in cards and "$30.00" in cards and "$120.50" in cards
        assert 'data-sortable="debts-people"' in r.text and 'data-sortable="debts-services"' in r.text
        for name, role in (("Dad", "Creditor"), ("Ana", "Debtor"), ("Leo", "Settled")):
            assert f"<strong>{name}</strong></td>\n        <td>{role}</td>" in r.text

    def test_month_filter(self, settings, client):
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write("2026-01-05;Card fee;Eq;20;Cc;dad\n2026-02-05;Card fee;Eq;30;Cc;Dad\n")
        snapshot = LedgerStore(settings).load()
        amounts = {
            month: snapshot.debt_accounts(month)[0].creditors["Dad"].amount
            for month in ("", "2025-12", "2026-01", "2026-02")
        }
        assert amounts == {"": 150, "2025-12": 100, "2026-01": 120, "2026-02": 150}
        assert snapshot.accounts["Cc"].creditors["Dad"].amount == 100  # left as loaded
        r = client.get("/debt", params={"month": "2026-01"})
        assert '<option value="2026-01" selected>' in r.text
        assert "by people, services and companies, as of end of 2026-01</small>" in r.text
        cards = r.text.split('<div class="cards">')[1].split("</div>\n  </div>")[0]
        assert "$170.50" in cards  # Dad 120 and Bank 50.50
        # The month is shared with the other tabs
        assert '<option value="2026-01" selected>' in client.get("/accounts").text
        client.get("/results", params={"month": "2025-12"})
        assert '<option value="2025-12" selected>' in client.get("/debt").text
        r = client.get("/debt", params={"month": "2026-02"}, headers=HTMX)
        assert "<html" not in r.text and '<div class="view">' in r.text and "$200.50" in r.text

    def test_movements(self, settings, client):
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write("2026-01-05;Card fee;Eq;20;Cc;dad\n2026-02-05;Card fee;Eq;30;Cc;no one\n")
        LedgerStore(settings).freeze_ids()
        r = client.get("/debt", params={"month": ""})
        movements = r.text.split('data-sortable="debt-movements"')[1].split("</table>")[0]
        # With their ids, the same as in the Ledger tab
        assert movement_ids(movements) == ["L-6", "L-5", "A-Cc"]
        assert set(movement_ids(movements)) <= set(movement_ids(client.get("/", params={"initial": True}).text))
        # The initial movement too, with the initial amounts of everyone
        assert "<h2>Movements <small>3, all time</small></h2>" in r.text
        assert movements.index("2026-02-05") < movements.index("2026-01-05") < movements.index("Initial (L)")
        assert "<strong>Dad</strong>" in movements and 'title="The comment names no one: unassigned debt">-' in movements
        assert ">Initial amounts</td>" in movements and 'data-sort="120.5"' in movements
        assert "Salary" not in movements  # not of a debt account
        r = client.get("/debt", params={"month": "2026-02"})
        assert "<h2>Movements <small>1, in 2026-02</small></h2>" in r.text

    def test_initial_amounts_count_from_the_initial_date(self, settings):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace("    description: Credit card\n", "    description: Credit card\n    initial_date: 2026-01-02\n"),
            encoding="utf-8",
        )
        snapshot = LedgerStore(settings).load()
        assert snapshot.debt_accounts("2025-12")[0].creditors["Dad"].amount == 0
        assert snapshot.debt_accounts("2026-01")[0].creditors["Dad"].amount == 100

    def test_no_alerts_when_all_is_as_expected(self, client):
        r = client.get("/debt")
        assert 'class="msg warn alert"' not in r.text
        assert '<td class="muted">-</td>' in r.text and "<td class=\"\">Creditor</td>" in r.text  # Dad, Bank

    def test_debt_bars_show_positive_amounts(self, client):
        r = client.get("/debt")
        owed = r.text.split("Owed to us <span")[1].split("<h2>👥 People")[0]
        assert '<span class="bar-name">Ana</span>' in owed and "-$30.00</span>" not in owed
        we_owe = r.text.split("We owe <span")[1].split("Owed to us <span")[0]
        assert '<span class="bar-name">Dad</span>' in we_owe
        assert "Leo" not in we_owe and "Ana" not in we_owe

    def test_ledger_debts_go_to_the_people_in_the_comment(self, settings, client):
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write(
                "2026-01-05;Card fee;Eq;20;Cc;Fee for dad\n"  # a credit: we owe more
                '2026-01-06;Move;Cc;"1,040.5";Ca;bank 2026-01\n'  # a debit: we owe less
                "2026-01-07;Move;Cc;5;Ca;Zurich 1/24\n"  # names no one
                "2026-01-08;Move;Ca;7;Bk;Dad\n"  # no debt account
            )
        settings.ledger_path.write_text(
            settings.ledger_path.read_text().replace("2026-01-04;Gift;Ca;10;", "2026-01-04;Gift;Ca;2000;")
        )
        [cc] = LedgerStore(settings).load().debt_accounts()
        # The initial amounts are only counted once, from the creditors YAML
        assert {n: c.amount for n, c in cc.creditors.items()} == {"Dad": 120, "Bank": -990, "Ana": -30, "Leo": 0}
        assert cc.unassigned == -5
        r = client.get("/debt")
        assert card_labels(r.text) == ["We owe", "Owed to us", "Unassigned", "Net debt"]
        cards = r.text.split('<div class="cards">')[1].split("</div>\n  </div>")[0]
        assert "$120.00" in cards and "$1,020.00" in cards and "-$5.00" in cards and "-$905.00" in cards
        accounts = r.text.split("<h2>Debt accounts</h2>")[1]
        assert 'data-sort="-5.0">-$5.00</td>' in accounts
        # Bank is a service, expected to be a creditor
        assert '<strong>Bank</strong></td>\n        <td class="nowrap hint warn" title="Expected to be a creditor">⚠ Debtor</td>' in r.text
        alerts = r.text.split('<details class="msg warn alert" role="alert">')[1:]
        assert len(alerts) == 2
        assert "Unassigned debt of" in alerts[0] and "<li>Zurich 1/24: <span class=\"money\">-$5.00</span></li>" in alerts[0]
        assert "1 is not in the expected role" in alerts[1] and "<strong>Bank</strong>" in alerts[1]
        assert "is a debtor (" in alerts[1] and "but is expected to be a creditor" in alerts[1]


@pytest.mark.parametrize(
    ("path", "message"),
    [("/investments", "No investment accounts yet"), ("/debt", "No debt accounts yet")],
)
def test_pages_without_special_accounts(client, path, message):
    r = client.get(path)
    assert r.status_code == 200 and message in r.text


def test_quantity_format():
    assert quantity(1000) == "1,000"
    assert quantity(1.5) == "1.5"
    assert quantity(0.123456) == "0.1235"
    assert quantity(0) == "0"


WORK_CSV = """\
Month;Company;Position;Work - Salary Main;Tax
2025-10;Acme;Acme Jr;1000.00;200.00
2025-11;Acme;Acme Sr;1500.50;300.50
2025-12;;;;
2026-01;Globex;GX Dev;2000.00;400.00"""


def with_salaries(settings, debit="Ca"):
    """
    The settings, with the base movement of the salaries of the work history debiting
    that account, and the Tax loss of their deductions.
    """
    path = settings.base_movements_path
    salary = f"        Work - Salary Main:\n          description: Main salary\n          debit: {debit}\n"
    text = path.read_text().replace("        Salary:\n", salary + "        Salary:\n", 1)
    path.write_text(text.replace("        Food:\n", "        Tax: Income taxes\n        Food:\n", 1))
    return settings


class TestWork:
    @pytest.fixture
    def settings(self, settings, tmp_path):
        path = tmp_path / "work.csv"
        path.write_text(WORK_CSV, encoding="utf-8")
        return replace(settings, work_path=path)

    @pytest.fixture
    def store(self, settings):
        return LedgerStore(settings)

    @pytest.fixture
    def client(self, settings):
        return TestClient(create_app(settings))

    def text(self, settings):
        return settings.work_path.read_text(encoding="utf-8")

    def test_load(self, store):
        work = store.load_work()
        assert [m.month for m in work.months] == ["2025-10", "2025-11", "2025-12", "2026-01"]
        assert work.months[2].is_gap
        assert list(work.companies) == ["Globex", "Acme"]
        assert work.current.position == "GX Dev"
        assert work.position_of(work.months[1]).name == "Acme Sr"

    def test_no_current_job_after_a_gap(self, store, settings):
        settings.work_path.write_text(WORK_CSV + "\n2026-02;;;;", encoding="utf-8")
        assert store.load_work().current is None

    @pytest.mark.parametrize(
        ("line", "error"),
        [
            ("2026-03;;;;", "2026-03 comes after 2026-01, but 2026-02 is expected"),
            ("2026-01;;;;", "is expected"),
            ("2026-02;Globex;;1;1", "both a company and a job position"),
            ("2026-02;;;1;1", "cannot have a salary"),
            ("2026-02;;;;1", "cannot have a salary"),
            ("2026-02;Globex;GX Dev;;", "must have a gross salary"),
            ("2026-02;Globex;GX Dev;1;2", "deductions of 2026-02 cannot be over its gross salary"),
            ("2026-02;Globex;GX Dev;1;x", "Invalid deduction 'Tax' of 2026-02"),
        ],
    )
    def test_data_errors(self, store, settings, line, error):
        settings.work_path.write_text(WORK_CSV + "\n" + line, encoding="utf-8")
        with pytest.raises(LedgerDataError, match=error):
            store.load_work()

    def test_deductions(self, store):
        work = store.load_work()
        assert work.deductions == ["Tax"]
        acme = work.months[1]
        assert (acme.gross, acme.deductions, acme.net) == (1500.5, {"Tax": 300.5}, 1200)
        assert work.months[2].net is None  # a gap

    def test_missing_columns(self, store, settings):
        settings.work_path.write_text("Month;Company\n2026-01;;\n", encoding="utf-8")
        with pytest.raises(LedgerDataError, match="missing the columns"):
            store.load_work()

    def test_add_after_the_last_month_keeps_the_file(self, store, settings):
        assert store.save_month("2026-02", "Globex", "GX Dev", "2,100", {"Tax": "400"}) is True
        assert self.text(settings) == WORK_CSV + "\n2026-02;Globex;GX Dev;2100.00;400.00"
        assert store.save_month("2026-03") is True  # a gap
        assert self.text(settings).endswith("\n2026-03;;;;")
        assert store.save_month("2026-04", "Globex", "GX Dev", "1") is True  # deductions left out are 0
        assert self.text(settings).endswith("\n2026-04;Globex;GX Dev;1.00;0.00")

    def test_add_before_the_first_month(self, store, settings):
        store.save_month("2025-09", "Acme", "Acme Jr", "900", {"Tax": "200"})
        assert self.text(settings).startswith(
            "Month;Company;Position;Work - Salary Main;Tax\n2025-09;Acme;Acme Jr;900.00;200.00\n2025-10;"
        )

    def test_existing_months_are_replaced(self, store, settings):
        assert store.save_month("2025-12", "Acme", "Acme Sr", "1500.5", {"Tax": "300.5"}) is False
        assert "\n2025-12;Acme;Acme Sr;1500.50;300.50\n" in self.text(settings)
        assert store.save_month("2025-11") is False  # now a gap
        assert "\n2025-11;;;;\n" in self.text(settings)
        assert len(store.load_work().months) == 4

    def test_payment_date_adds_its_column(self, store, settings):
        store.save_month("2025-11", "Acme", "Acme Sr", "1500.5", {"Tax": "300.5"}, "2025-12-05")
        assert self.text(settings) == (
            "Month;Company;Position;Work - Salary Main;Tax;Received payment date\n"
            "2025-10;Acme;Acme Jr;1000.00;200.00;\n"
            "2025-11;Acme;Acme Sr;1500.50;300.50;2025-12-05\n"
            "2025-12;;;;;\n"
            "2026-01;Globex;GX Dev;2000.00;400.00;"
        )
        assert store.load_work().months[1].payment_date == dt.date(2025, 12, 5)
        store.save_month("2026-02", "Globex", "GX Dev", "1", {"Tax": "1"})
        assert self.text(settings).endswith("\n2026-02;Globex;GX Dev;1.00;1.00;")

    @pytest.mark.parametrize(
        ("args", "error"),
        [
            (("2026-03",), "Months cannot be skipped: add 2026-02 \\(after the last month\\) first"),
            (("2025-08",), "Months cannot be skipped"),
            (("2026-02", "Globex", "", "1", {"Tax": "1"}), "both a company and a job position"),
            (("2026-02", "", "", "1"), "cannot have a salary"),
            (("2026-02", "", "", "", {"Tax": "1"}), "cannot have a salary"),
            (("2026-02", "Globex", "GX Dev", "", {"Tax": "1"}), "must have a gross salary"),
            (("2026-02", "Globex", "GX Dev", "1", {"Tax": "2"}), "cannot be over its gross salary"),
            (("2026-02", "Globex", "GX Dev", "1", {"Tax": "-1"}), "cannot be negative"),
            (("2026-02", "Globex", "GX Dev", "1", {"Tax": "x"}), "Invalid deduction 'Tax'"),
            (("2026-02", "Globex", "GX Dev", "1", {"Month": "1"}), "Invalid deduction of the salary: 'Month'"),
            (("2026-02", "Globex", "GX Dev", "1", {"A;B": "1"}), "Invalid deduction of the salary"),
            (("2026-02", "Globex", "GX Dev", "1", {"Tax": "1"}, "2026-02-30"), "Invalid received payment date"),
            (("June",), "must be YYYY-MM"),
        ],
    )
    def test_save_errors(self, store, settings, args, error):
        with pytest.raises(ValueError, match=error):
            store.save_month(*args)
        assert self.text(settings) == WORK_CSV

    def test_delete_first_or_last_month_only(self, store, settings):
        rows = store.load_work().rows
        with pytest.raises(ValueError, match="Only the first or last month"):
            store.delete_month(1, work_fingerprint(rows.iloc[1]))
        store.delete_month(3, work_fingerprint(rows.iloc[3]))
        store.delete_month(0, work_fingerprint(rows.iloc[0]))
        assert [m.month for m in store.load_work().months] == ["2025-11", "2025-12"]

    def test_page(self, client):
        r = client.get("/work")
        assert r.status_code == 200
        assert 'href="/work" aria-current="page"' in r.text
        assert card_labels(r.text) == ["Current job", "Gross", "Deductions", "Net"]
        assert "<strong>Globex</strong> <span class=\"badge gain\">Current</span>" in r.text
        salaries = r.text.split('data-sortable="salaries"')[1]
        assert re.findall(r"<strong>(\d{4}-\d{2})</strong>", salaries) == ["2026-01", "2025-12", "2025-11", "2025-10"]
        assert '<td class="muted" colspan="2">No job</td>' in salaries
        assert "$1,500.50" in salaries
        # Gross, deductions (itemized on hover) and net
        assert '<td class="num money " data-sort="300.5"\n            title="Tax: $300.50">$300.50</td>' in salaries
        assert '<td class="num money" data-sort="1200.0">$1,200.00</td>' in salaries
        # Only the first and last months have a delete button
        assert salaries.count('aria-label="Delete') == 2
        # The form is ready for the next month, at the last job
        assert "<h2>New month</h2>" in r.text
        assert 'name="month" value="2026-02"' in r.text and 'name="position" value="GX Dev"' in r.text

    def test_unpaid_months_warning(self, client, store, settings):
        # Worked months already over: 2025-10, 2025-11 and 2026-01 (2025-12 is a gap)
        r = client.get("/work")
        assert "⚠ 3 past months with a job have no received payment date: 2026-01, 2025-11, 2025-10." in r.text
        assert r.text.count("⚠ Not received") == 3
        store.save_month("2025-11", "Acme", "Acme Sr", "1500.5", {"Tax": "300.5"}, "2025-12-05")
        store.save_month("2026-01", "Globex", "GX Dev", "2000", {"Tax": "400"}, "2026-02-05")
        r = client.get("/work")
        assert "⚠ 1 past month with a job has no received payment date: 2025-10." in r.text
        store.save_month("2025-10", "Acme", "Acme Jr", "1000", {"Tax": "200"}, "2025-11-05")
        assert "no received payment date" not in client.get("/work").text

    def test_current_month_is_not_unpaid(self, client, store, settings):
        today = dt.date.today()
        settings.work_path.write_text(
            "Month;Company;Position;Work - Salary Main;Received payment date\n"
            f"{today:%Y-%m};Acme;Dev;10;\n",
            encoding="utf-8",
        )
        r = client.get("/work")
        assert "no received payment date" not in r.text and "⚠ Not received" not in r.text

    def test_edit_form(self, client):
        r = client.get("/work/form", params={"month": "2025-11"})
        assert "<h2>Edit 2025-11</h2>" in r.text and 'value="1,500.50"' in r.text
        assert 'name="deduction:Tax" inputmode="decimal" value="300.50"' in r.text
        assert "Save 2025-11" in r.text
        r = client.get("/work/form", params={"month": "2026-02"})
        assert "<h2>New month</h2>" in r.text

    def test_forms(self, client, store):
        r = client.post("/work/months", data={"month": "2026-03"}, headers=HTMX)
        assert "Months cannot be skipped" in r.text and 'value="2026-03"' in r.text
        r = client.post(
            "/work/months",
            data={"month": "2026-02", "company": "Globex", "position": "GX Dev", "gross": "2,000.00", "deduction:Tax": "400"},
            headers=HTMX,
        )
        assert "Added 2026-02 (GX Dev at Globex)" in r.text
        assert store.load_work().months[-1].deductions == {"Tax": 400}
        r = client.post(
            "/work/months",
            data={"month": "2026-03", "company": "Globex", "position": "GX Dev", "gross": "1", "deduction:Tax": "2"},
            headers=HTMX,
        )
        assert "cannot be over its gross salary" in r.text and 'name="deduction:Tax" inputmode="decimal" value="2"' in r.text
        assert 'name="month" value="2026-03"' in r.text  # ready for the next one
        r = client.post("/work/months", data={"month": "2025-12"}, headers=HTMX)
        assert "Updated 2025-12 (no job)" in r.text
        fingerprint = work_fingerprint(store.load_work().rows.iloc[1])
        r = client.post("/work/months/1/delete", data={"fingerprint": fingerprint}, headers=HTMX)
        assert r.status_code == 422
        r = client.post("/work/months/0/delete", data={"fingerprint": "stale"}, headers=HTMX)
        assert r.status_code == 409

    def test_api(self, client):
        acme = client.get("/api/work/companies").json()[1]
        assert acme == {
            "name": "Acme", "start": "2025-10", "end": "2025-11",
            "positions": [
                {"name": "Acme Sr", "start": "2025-11", "end": "2025-11", "months": 1},
                {"name": "Acme Jr", "start": "2025-10", "end": "2025-10", "months": 1},
            ],
        }
        body = {"month": "2026-02", "company": "Globex", "position": "GX Dev", "gross": 10, "deductions": {"Tax": 2}}
        r = client.put("/api/work/months", json=body)
        assert r.status_code == 200 and r.json()["created"] is True
        assert client.get("/api/work/months").json()[-1] == {
            "month": "2026-02", "company": "Globex", "position": "GX Dev",
            "gross": 10.0, "deductions": {"Tax": 2.0}, "net": 8.0, "payment_date": None,
        }
        r = client.put("/api/work/months", json={"month": "2026-04"})
        assert r.status_code == 422 and "cannot be skipped" in r.json()["detail"]


def test_work_file_is_created_when_adding(settings, tmp_path):
    settings = replace(settings, work_path=tmp_path / "w" / "work.csv")
    r = TestClient(create_app(settings)).get("/work")
    assert r.status_code == 200 and "No companies yet" in r.text and "No months yet." in r.text
    store = LedgerStore(settings)
    store.save_month("2025-01", "Acme", "Dev", "10", {"Food": "2"}, "2025-02-01")
    assert settings.work_path.read_text(encoding="utf-8") == (
        "Month;Company;Position;Work - Salary Main;Received payment date;Food\n"
        "2025-01;Acme;Dev;10.00;2025-02-01;2.00\n"
    )


def test_salary_charts(settings, tmp_path):
    path = tmp_path / "work.csv"
    path.write_text(WORK_CSV, encoding="utf-8")
    r = TestClient(create_app(replace(settings, work_path=path))).get("/work")
    for title in ("Gross salary", "Net salary"):
        assert f"<h2>{title} <small>per month, 0 without a job</small></h2>" in r.text
    points = [json.loads(html.unescape(p)) for p in re.findall(r"data-points='([^']*)'", r.text)]
    gross, net = points
    assert [(p["label"], p["value_text"]) for p in net] == [
        ("2025-10", "$800.00"), ("2025-11", "$1,200.00"), ("2025-12", "$0.00"), ("2026-01", "$1,600.00"),
    ]
    assert gross[2]["change_text"] == "No job" and gross[0]["change_text"] == "Acme · Acme Jr"
    assert gross[2]["y"] == 100  # the no-job month is at 0, on the axis
    # Each salary in its own color: the net one green
    assert re.findall(r'class="line-chart" [^>]*style="--line: var\((--series-\d)\)"', r.text) == ["--series-1", "--series-3"]
    # The real salaries, at the CPI of the end of each month: none before base_currency.csv
    assert net[0]["real_text"] == "-" and net[-1]["real_text"] == "$1,600.00"
    assert r.text.count("(prices of 2026-01-31)") == 2


def test_work_form_fills_in_from_the_last_month(settings, tmp_path):
    path = tmp_path / "work.csv"
    path.write_text(
        "Month;Company;Position;Work - Salary Main;Tax;Received payment date\n"
        "2025-12;Acme;Acme Sr;1500.50;300.50;2026-01-05\n"
        "2026-01;Globex;GX Dev;2000.00;400.00;2026-02-05\n",
        encoding="utf-8",
    )
    r = TestClient(create_app(replace(settings, work_path=path))).get("/work")
    form = r.text.split('id="work-form"')[1].split("</form>")[0]
    (filled,) = re.findall(r"data-autofill='([^']*)'", form)
    assert json.loads(html.unescape(filled)) == {
        "company": "Globex", "position": "GX Dev", "gross": "2,000.00", "deduction:Tax": "400.00",
    }  # not the month nor the received payment date
    assert "Fill in from 2026-01" in form and 'value="2026-02"' in form
    # Not when editing a month: it already has its values
    form = TestClient(create_app(replace(settings, work_path=path))).get("/work/form", params={"month": "2025-12"}).text
    assert "data-autofill" not in form


def test_no_salary_charts_without_months(client):
    assert "Gross salary <small>" not in client.get("/work").text


class TestDebtKinds:
    @pytest.fixture
    def settings(self, settings, tmp_path):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace("    description: Credit card\n    initial_amount: 0\n", "    description: Credit card\n"),
            encoding="utf-8",
        )
        creditors = tmp_path / "creditors.yaml"
        creditors.write_text(
            "Cc:\n  people:\n    Dad: 100\n  services:\n    Gas: 20\n  companies:\n    Acme: -5\n    Old Co: 3\n",
            encoding="utf-8",
        )
        work = tmp_path / "work.csv"
        work.write_text(WORK_CSV, encoding="utf-8")
        return replace(with_salaries(settings), creditors_path=creditors, work_path=work)

    def test_companies_come_from_the_work_history(self, settings):
        before = settings.creditors_path.read_text(encoding="utf-8")
        cc = LedgerStore(settings).load().accounts["Cc"]
        assert {n: (c.kind.value, c.amount) for n, c in cc.creditors.items()} == {
            "Dad": ("people", 100), "Gas": ("services", 20),
            "Acme": ("companies", -5), "Old Co": ("companies", 3), "Globex": ("companies", 0),
        }
        assert cc.initial_amount == 118
        assert settings.creditors_path.read_text(encoding="utf-8") == before  # never written

    def test_new_companies_of_the_work_history(self, settings):
        store = LedgerStore(settings)
        store.save_month("2026-02", "Initech", "Dev", "10", {"Tax": "2"})
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write("2026-02-05;Card fee;Eq;7;Cc;initech bonus\n")
        assert store.load().debt_accounts()[0].creditors["Initech"].amount == 7

    def test_a_company_cannot_be_another_kind(self, settings):
        text = settings.creditors_path.read_text(encoding="utf-8")
        settings.creditors_path.write_text(text.replace("    Gas: 20\n", "    Gas: 20\n    Globex: 1\n"), encoding="utf-8")
        with pytest.raises(LedgerDataError, match="'Globex' of the account 'Cc' is a company of the work history"):
            LedgerStore(settings).load()

    def test_debt_page_by_kind(self, settings):
        r = TestClient(create_app(settings)).get("/debt")
        assert r.status_code == 200
        for title, count in (("👥 People", 1), ("🧾 Services", 1), ("🏢 Companies", 3)):
            assert f"<h2>{title} <small>{count}</small></h2>" in r.text
        companies = r.text.split("<h2>🏢 Companies")[1].split("</table>")[0]
        assert "<strong>Globex</strong></td>\n        <td>Settled</td>" in companies
        assert "<strong>Dad</strong>" not in companies
        we_owe = r.text.split("We owe <span")[1].split("Owed to us <span")[0]
        assert '<span class="bar-name">Services</span>' in we_owe and '<span class="bar-name">Gas</span>' in we_owe
        owed = r.text.split("Owed to us <span")[1].split("<h2>👥 People")[0]
        assert '<span class="bar-name">Companies</span>' in owed and "People</span>" not in owed


class TestSalaries:
    """The salaries of the work history are generated each week, never stored in the ledger CSV."""

    @pytest.fixture
    def settings(self, settings, tmp_path):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace("    description: Credit card\n    initial_amount: 0\n", "    description: Credit card\n"),
            encoding="utf-8",
        )
        creditors = tmp_path / "creditors.yaml"
        creditors.write_text("Cc:\n  companies:\n    Acme: -5\n", encoding="utf-8")
        work = tmp_path / "work.csv"
        work.write_text(WORK_CSV, encoding="utf-8")
        return replace(with_salaries(settings, debit="Cc"), creditors_path=creditors, work_path=work)

    def salaries(self, settings) -> list[tuple]:
        rows = LedgerStore(settings).load().salary_rows
        return list(rows[["move_date", "base_movement", "acc_debit", "amount", "acc_credit", "comment"]].itertuples(index=False))

    def test_each_week_of_a_month_from_the_start_of_the_ledger(self, settings):
        # 2026-01 has 5 weeks, from Monday 2025-12-29 (Thursday 2026-01-01) on, but the
        # ledger starts on 2026-01-01: the months of Acme, and that week, are left out.
        # Each salary is followed by its deductions, split the same way, out of its account.
        assert self.salaries(settings) == [
            row
            for i, day in enumerate(["2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26"], 2)
            for row in (
                (day, "Work - Salary Main", "Cc", "400.00", "Eq", f"Globex (GX Dev): week {i}/5 of 2026-01"),
                (day, "Tax", "Eq", "80.00", "Cc", f"Globex (GX Dev): Tax, week {i}/5 of 2026-01"),
            )
        ]
        LedgerStore(settings).save_month("2026-02", "Globex", "GX Dev", "1000.01", {"Tax": "200.01"})
        assert [(r[0], r[1], r[3]) for r in self.salaries(settings)[8:]] == [
            row
            for day, salary, tax in (
                ("2026-02-02", "250.01", "50.01"), ("2026-02-09", "250.00", "50.00"),
                ("2026-02-16", "250.00", "50.00"), ("2026-02-23", "250.00", "50.00"),
            )
            for row in ((day, "Work - Salary Main", salary), (day, "Tax", tax))
        ]

    def test_are_in_the_ledger_and_owed_by_the_company(self, settings):
        snapshot = LedgerStore(settings).load()
        # The net salary: 1,600 of salary minus 320 of tax, with the initial -5 of Acme
        assert snapshot.ledger().balances()["Cc"] == -1285
        cc = snapshot.debt_accounts()[0]
        assert (cc.creditors["Globex"].amount, cc.creditors["Acme"].amount, cc.unassigned) == (-1280, -5, 0)
        assert snapshot.debt_accounts("2026-01")[0].creditors["Globex"].amount == -1280
        # Each salary's id is its month and week, and each deduction's also names it, so
        # editing other months or columns never changes them
        tax = deduction_id("Tax")
        assert [snapshot.ids[ix] for ix in snapshot.salary_rows.index] == [
            move_id for i in range(2, 6) for move_id in (f"W-2026-01-{i}", f"W-2026-01-{i}-{tax}")
        ]
        r = TestClient(create_app(settings)).get("/")
        # Each links to the row of its month in the Work tab
        assert r.text.count('href="/work#month-2026-01" title="Generated each week from the salary of the Work tab" aria-label="Generated each week from the salary of the Work tab">🟠</a>') == 8
        assert '<tr id="month-2026-01">' in TestClient(create_app(settings)).get("/work").text
        assert r.text.count('aria-label="Delete movement"') == 4  # only the CSV rows

    def test_cannot_be_stored_or_added_by_hand(self, settings):
        store = LedgerStore(settings)
        with pytest.raises(ValueError, match="'Work - Salary Main' are generated by pledger"):
            store.add("2026-01-05", "Work - Salary Main", "Cc", "Eq", "1", "Globex")
        assert "Work - Salary Main" not in TestClient(create_app(settings)).get("/form/bases", params={"move_type": "gain"}).text
        with settings.ledger_path.open("a") as f:
            f.write("2026-01-05;Work - Salary Main;Cc;1;Eq;Globex\n")
        with pytest.raises(LedgerDataError, match=re.escape("cannot have movements of ['Work - Salary Main']")):
            store.load()

    @pytest.mark.parametrize(
        "salary",
        ["        Work - Salary Main:\n          description: Main salary\n          debit: Cc\n",
         "          debit: Cc\n"],  # no fixed debit account
    )
    def test_their_base_movement_is_required(self, settings, salary):
        path = settings.base_movements_path
        path.write_text(path.read_text().replace(salary, "", 1))
        with pytest.raises(LedgerDataError, match=re.escape("'Work - Salary Main' is required for the salaries")):
            LedgerStore(settings).load()
        # Not without salaries to generate
        settings.work_path.write_text(WORK_CSV.rsplit("\n", 1)[0], encoding="utf-8")
        LedgerStore(settings).load()

    @pytest.mark.parametrize(
        ("column", "error"),
        [
            ("Taxes", "The column 'Taxes' of the work CSV is not a base movement"),
            ("Gift", "The column 'Gift' of the work CSV must be a deduction of the salary"),  # a gain
            ("Card fee", "The column 'Card fee' of the work CSV must be a deduction"),  # credits Cc
            ("Initial (L)", "The column 'Initial \\(L\\)' of the work CSV must be a deduction"),  # internal
        ],
    )
    def test_deductions_must_be_losses_out_of_the_salary(self, settings, column, error):
        settings.work_path.write_text(WORK_CSV.replace(";Tax\n", f";{column}\n", 1), encoding="utf-8")
        if column == "Card fee":  # fine while Cc is the account of the salary, but not of another one
            LedgerStore(settings).load()
            path = settings.base_movements_path
            path.write_text(path.read_text().replace("          debit: Cc\n", "          debit: Ca\n", 1))
        with pytest.raises(LedgerDataError, match=error):
            LedgerStore(settings).load()

    def test_a_new_deduction_adds_its_column(self, settings):
        store = LedgerStore(settings)
        store.save_month("2026-02", "Globex", "GX Dev", "1000", {"Tax": "200", "Food": "100"})
        assert settings.work_path.read_text(encoding="utf-8") == (
            "Month;Company;Position;Work - Salary Main;Tax;Food\n"
            "2025-10;Acme;Acme Jr;1000.00;200.00;0.00\n"
            "2025-11;Acme;Acme Sr;1500.50;300.50;0.00\n"
            "2025-12;;;;;\n"
            "2026-01;Globex;GX Dev;2000.00;400.00;0.00\n"
            "2026-02;Globex;GX Dev;1000.00;200.00;100.00"
        )
        assert store.load().work.months[-1].net == 700
        # Not of a base movement that can't be a deduction
        before = settings.work_path.read_text(encoding="utf-8")
        with pytest.raises(ValueError, match="Could not save 2026-02: .* 'Gift' of the work CSV must be a deduction"):
            store.save_month("2026-02", "Globex", "GX Dev", "1000", {"Gift": "1"})
        assert settings.work_path.read_text(encoding="utf-8") == before

    def test_work_tab_from_gross_to_net(self, settings):
        today = dt.date.today()
        settings.work_path.write_text(
            f"Month;Company;Position;Work - Salary Main;Tax;Food\n{today:%Y-%m};Globex;GX Dev;2000.00;400.00;0.00\n",
            encoding="utf-8",
        )
        r = TestClient(create_app(settings)).get("/work")
        assert f"<h2>From gross to net <small>{today.year}</small></h2>" in r.text
        table = r.text.split("<h2>From gross to net")[1].split("</table>")[0]
        assert "<td>Tax</td>" in table and "20.0%" in table and "<td>Food</td>" not in table  # none this year
        # From the gross salary on top down to the net one, not sortable
        assert table.index("<th>Gross salary</th>") < table.index("<td>Tax</td>") < table.index("<th>Net salary</th>")
        assert '<th class="num money">$2,000.00</th>' in table and '<th class="num money">$1,600.00</th>' in table
        assert "data-sortable" not in table
        assert 'name="deduction:Tax"' in r.text


INVEST_YAML = (
    "  Invest:\n    id: 4\n    long_name: Investments\n    description: d\n"
    "    base_movements:\n      gain:\n        Capital gains:\n          description: d\n"
    "          debit: INVESTMENT\n        Dividends: d\n      loss:\n        Broker fees: d\n"
    "        Capital losses:\n          description: d\n          credit: INVESTMENT\n"
    "  transfer:\n    Investment - Bought:\n      description: d\n      debit: INVESTMENT\n"
    "    Investment - Sold:\n      description: d\n      credit: INVESTMENT\n"
    "    Adjust: Set by pledger\n"
)
# The internal base movements of the Debt tab; appended to the `internal` section of INITIAL_YAML
DEBT_YAML = (
    "  transfer:\n"
    "    Debt - Incurred:\n      description: d\n      credit: DEBT\n"
    "    Debt - Paid:\n      description: d\n      debit: DEBT\n"
    "    Loan - Given:\n      description: d\n      debit: DEBT\n"
    "    Loan - Collected:\n      description: d\n      credit: DEBT\n"
)
INVESTMENT_HEADER = "move_date;base_movement;acc_debit;amount;acc_credit;quantity;investment;comment\n"


def investment_rows(settings):
    return LedgerStore(settings).load().investments


class TestNewInvestment:
    @pytest.fixture
    def settings(self, settings, tmp_path):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace("liability:\n", "  Br:\n    description: Broker\nliability:\n"), encoding="utf-8"
        )
        path = tmp_path / "investment.yaml"
        path.write_text("Br:\n  T: stock\n", encoding="utf-8")
        return replace(settings, investments_path=path)

    def test_add_investment(self, settings):
        store = LedgerStore(settings)
        store.add_investment("Br", " SPY ", "etf")
        store.add_investment("Br", "AL30", "public_bond", "1,500", "10")
        assert settings.investments_path.read_text() == (
            "Br:\n  T: stock\n  SPY: etf\n  AL30:\n    type: public_bond\n    amount: 1500.0\n    quantity: 10.0\n"
        )
        held = store.load().accounts["Br"]
        assert held.investments["AL30"]["total_amount"] == 1500 and held.initial_amount == 1500
        for args, error in [
            (("Br", "T", "etf"), "Investment 'T' already exists"),
            (("Ca", "X", "etf"), "'Ca' is not an investment account"),
            (("Br", "X", "gold"), "Unknown investment type: 'gold'"),
            (("Br", " ", "etf"), "name is required"),
            (("Br", "X", "etf", "-1"), "cannot be negative"),
        ]:
            with pytest.raises(ValueError, match=re.escape(error)):
                store.add_investment(*args)
        assert "X" not in settings.investments_path.read_text()

    def test_new_investment_form(self, settings):
        client = TestClient(create_app(settings))
        r = client.get("/investments")
        assert "<h2>New investment</h2>" in r.text  # even without investment base movements
        assert "<h2>New investment movement</h2>" not in r.text
        form = r.text.split('id="new-investment-form"')[1].split("</form>")[0]
        assert "<option selected>Br</option>" in form or "<option >Br</option>" in form
        assert '<option value="etf"' in form and 'name="amount"' in form and 'name="quantity"' in form
        r = client.post("/investments/new", data={"account": "Br", "name": "T", "investment_type": "etf"})
        assert 'class="msg error"' in r.text and 'value="T"' in r.text  # kept in the form
        assert client.post("/api/investments", json={"account": "Br", "name": "SPY", "type": "etf"}).status_code == 201
        assert client.post("/api/investments", json={"account": "Br", "name": "SPY", "type": "etf"}).status_code == 422
        fields = client.get("/form/invest-fields", params={"base_movement": "Capital gains"}).text
        assert "<option >SPY</option>" in fields


class TestInvestMovements:
    """
    The internal Invest base movements are handled in the Investments tab, not the Ledger
    tab, with their movements in the investments CSV and the interest one.
    """

    @pytest.fixture
    def settings(self, settings, tmp_path):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace("liability:\n", "  Br:\n    description: Broker\nliability:\n"), encoding="utf-8"
        )
        path = settings.base_movements_path
        path.write_text(path.read_text().replace("internal:\n", "internal:\n" + INVEST_YAML, 1))
        # A capital gain of T
        movements = tmp_path / "investment.csv"
        movements.write_text(INVESTMENT_HEADER + "2026-01-05;Capital gains;Br;5;Eq;;T;\n")
        investments = tmp_path / "investment.yaml"
        investments.write_text("Br:\n  T:\n    type: stock\n    amount: 100\n    quantity: 10\n", encoding="utf-8")
        return replace(
            settings, investments_path=investments, investment_movements_path=movements,
            interest_path=tmp_path / "interest.csv",
        )

    @pytest.fixture
    def client(self, settings):
        return TestClient(create_app(settings))

    @staticmethod
    def append(settings, text):
        with settings.investment_movements_path.open("a") as f:
            f.write(text)

    def test_edit_investment_movement(self, settings, client):
        LedgerStore(settings).freeze_ids()
        fp = row_fingerprint(LedgerStore(settings).load().investments.iloc[0])
        assert 'hx-get="/investments/movements/0/edit"' in client.get("/investments").text
        r = client.get("/investments/movements/0/edit", params={"fingerprint": fp})
        assert "Edit investment movement" in r.text and "<small>I-1</small>" in r.text
        assert 'hx-post="/investments/movements/0/edit"' in r.text and 'value="5.00"' in r.text
        r = client.post("/investments/movements/0/edit", data={
            "fingerprint": fp, "move_date": "2026-01-05", "move_type": "gain", "base_movement": "Capital gains",
            "acc_debit": "Br", "acc_credit": "Eq", "amount": "7", "investment": "T",
        })
        assert "Updated I-1 Capital gains of T: $7.00" in r.text and r.headers["HX-Trigger"] == "investments-changed"
        assert settings.investment_movements_path.read_text().splitlines()[-1] == "2026-01-05;Capital gains;Br;7.00;Eq;;T;;I-1"
        assert client.get("/investments/movements/0/edit", params={"fingerprint": fp}).status_code == 409
        r = client.post("/investments/movements/0/edit", data={
            "fingerprint": row_fingerprint(LedgerStore(settings).load().investments.iloc[0]), "move_date": "2026-01-05",
            "base_movement": "Capital gains", "acc_debit": "Br", "acc_credit": "Eq", "amount": "7", "investment": "Nope",
        })
        assert 'class="msg error"' in r.text and "Edit investment movement" in r.text

    def test_investment_filter(self, settings, client):
        settings.investments_path.write_text(settings.investments_path.read_text() + "  U:\n    type: etf\n    amount: 50\n    quantity: 1\n")
        self.append(settings, "2026-01-06;Capital gains;Br;3;Eq;;U;\n")
        r = client.get("/investments")
        assert '<form id="invest-filter"' in r.text and '<optgroup label="Br">' in r.text
        assert """<option value='["Br", "U"]' >U</option>""" in r.text
        r = client.get("/investments/movements", params={"investment": '["Br", "U"]'})
        assert "<h2>Investment movements <small>1</small></h2>" in r.text and "$3.00" in r.text
        for bad in ("", "nope", '["Br"]', '["Ca", "U"]'):
            n = 0 if bad == '["Ca", "U"]' else 2
            assert f"<h2>Investment movements <small>{n}</small></h2>" in client.get("/investments/movements", params={"investment": bad}).text
        # The movements reload with the investment picked
        assert 'hx-include="#invest-filter"' in client.get("/investments").text

    def test_broker_fees_over_amount_and_totals_row(self, settings, client):
        settings.investments_path.write_text(settings.investments_path.read_text() + "  U:\n    type: etf\n    amount: 50\n    quantity: 1\n")
        # T: 1 of fees, of its 105 of amount; U: 2 of fees, of its 50
        self.append(settings, "2026-01-06;Broker fees;Eq;1;Ca;;T;\n2026-01-06;Broker fees;Eq;2;Ca;;U;\n")
        data = build_investments(LedgerStore(settings).load())
        assert data["has_fees"]
        assert {h["name"]: h["fees_share"] for h in data["holdings"]} == {"T": pytest.approx(1 / 105), "U": pytest.approx(0.04)}
        assert data["held"]["amount"] == pytest.approx(155) and data["held"]["fees_share"] == pytest.approx(3 / 155)
        table = client.get("/investments").text.split('data-sortable="investments"')[1].split("</table>")[0]
        assert "Fees over amount</th>" in table and "0.95%</td>" in table and "4.00%</td>" in table
        foot = table.split("<tfoot>")[1]
        assert '<th colspan="5">Total</th>' in foot and "$155.00" in foot  # 105 + 50 and "150.0%</td>" in foot

    def test_are_added_only_from_the_investments_tab(self, settings, client):
        store = LedgerStore(settings)
        with pytest.raises(ValueError, match="added from the Investments tab"):
            store.add("2026-02-01", "Broker fees", "Eq", "Ca", "1")
        with pytest.raises(ValueError, match="not an investment base movement"):
            store.add("2026-02-01", "Food", "Eq", "Ca", "1", invest=True)
        assert "Broker fees" not in client.get("/form/bases", params={"move_type": "loss"}).text

        data = {"move_date": "2026-02-01", "base_movement": "Broker fees",
                "acc_debit": "Eq", "acc_credit": "Ca", "amount": "1.5"}
        r = client.post("/investments/movements", data=data, headers=HTMX)
        assert "Added Broker fees: $1.50" in r.text and r.headers["HX-Trigger"] == "investments-changed"
        body = {**data, "base_movement": "Capital gains", "acc_debit": "Br", "acc_credit": "Eq", "amount": 2,
                "investment": "T"}
        assert client.post("/api/investments/movements", json=body).status_code == 201
        assert client.post("/api/movements", json=body).status_code == 422
        assert list(investment_rows(settings).base_movement) == ["Capital gains", "Broker fees", "Capital gains"]
        assert len(rows(settings)) == 4  # the ledger CSV is left as is
        # The INVESTMENT side still takes only investment accounts
        r = client.post("/investments/movements", data={**data, "base_movement": "Capital gains",
                                                         "acc_debit": "Ca", "acc_credit": "Eq", "investment": "T"})
        assert "must be an investment account" in r.text

    def test_are_deleted_only_from_the_investments_tab(self, settings, client):
        fp = row_fingerprint(investment_rows(settings).iloc[0])
        assert client.post("/investments/movements/0/delete", data={"fingerprint": "x"}).status_code == 409
        assert client.post("/investments/movements/1/delete", data={"fingerprint": fp}).status_code == 409
        r = client.post("/investments/movements/0/delete", data={"fingerprint": fp})
        assert r.status_code == 200 and r.headers["HX-Trigger"] == "investments-changed"
        assert len(investment_rows(settings)) == 0 and len(rows(settings)) == 4
        assert settings.investment_movements_path.read_text() == INVESTMENT_HEADER

    def test_cannot_be_in_the_ledger_csv(self, settings):
        with settings.ledger_path.open("a") as f:
            f.write("2026-01-05;Broker fees;Eq;1;Ca;\n")
        with pytest.raises(LedgerDataError, match=re.escape("cannot have movements of ['Broker fees']: they go in")):
            LedgerStore(settings).load()

    def test_deleting_cannot_break_the_investments(self, settings):
        self.append(settings, "2026-01-06;Investment - Bought;Br;10;Ca;2;T;\n2026-01-07;Investment - Sold;Ca;12;Br;12;T;\n")
        store = LedgerStore(settings)
        with pytest.raises(ValueError, match=r"quantity of 'T' .* cannot be negative"):
            store.delete_investment(1)  # the 2 bought, sold later

    def test_investments_tab_lists_them(self, client):
        r = client.get("/investments")
        assert 'id="invest-form"' in r.text
        assert r.text.index("<h2>New investment</h2>") < r.text.index("<h2>New investment movement</h2>")
        result = r.text.split("<h2>Total result <small>")[1].split("</section>")[0]
        assert "--line: var(--series-3)" in result  # green
        types = r.text.split('id="invest-form"')[1].split('class="segmented types"')[1].split("</div>")[0]
        assert re.findall(r'value="(\w+)" checked', types) == ["loss"]  # a loss by default
        assert 'hx-get="/form/invest-bases" hx-target="#invest-bases"' in types
        bases = r.text.split('id="invest-bases"')[1].split("</select>")[0]
        assert "Broker fees" in bases and "Capital gains" not in bases
        bases = client.get("/form/invest-bases", params={"move_type": "gain"}, headers=HTMX).text
        assert "<option selected>Capital gains</option>" in bases and "Broker fees" not in bases
        assert 'id="invest-fields"' in bases
        assert "<h2>Investment movements <small>1</small></h2>" in r.text
        assert client.get("/api/investments/movements").json()[0]["id"] is None  # until frozen
        LedgerStore(client.app.state.store.settings).freeze_ids()
        assert movement_ids(client.get("/investments").text) == ["I-1"]
        assert "I-1" in movement_ids(client.get("/").text)
        assert 'hx-post="/investments/movements/0/delete"' in r.text
        assert client.get("/api/investments/movements").json()[0]["base"] == "Capital gains"
        assert client.get("/api/investments/movements").json()[0]["source"] == "investments"
        # The ledger shows them with a lock that leads to the Investments tab
        index = client.get("/").text
        assert 'href="/investments#movement-I-1" title="Added, edited and deleted from the Investments tab" aria-label="Added, edited and deleted from the Investments tab">🟢</a>' in index
        assert '<tr id="movement-I-1">' in client.get("/investments").text
        assert index.count('aria-label="Delete movement"') == 4

    def test_interest_on_money(self, settings, client):
        assert card_labels(client.get("/investments").text) == ["Invested", "Net capital gains", "Total result"]
        path = settings.base_movements_path
        path.write_text(
            path.read_text()
            .replace("Dividends: d\n", "Dividends: d\n        Interest on Money (G): d\n", 1)
            .replace("Broker fees: d\n", "Broker fees: d\n        Interest on Money (L): d\n", 1)
        )
        r = client.get("/investments")
        assert card_labels(r.text) == ["Invested", "Net interest on money", "Net capital gains", "Total result"]
        assert "No interest on money yet." in r.text
        # Added to the interest CSV, created if missing: interest paid is negative
        f = client.get("/form/invest-fields", params={"base_movement": "Interest on Money (L)"}).text
        assert 'name="investment"' not in f
        data = {"move_date": "2026-02-10", "base_movement": "Interest on Money (L)",
                "acc_debit": "Eq", "acc_credit": "Bk", "amount": "1"}
        assert "Added Interest on Money (L): $1.00" in client.post("/investments/movements", data=data).text
        assert settings.interest_path.read_text() == "move_date;acc_debit;amount;comment;id\n2026-02-10;Bk;-1.00;;M-1\n"
        with pytest.raises(ValueError, match="Only movements of the investments have an investment"):
            LedgerStore(settings).add(**{**data, "investment": "T"}, invest=True)
        with settings.interest_path.open("a") as f:
            f.write("2026-01-10;Ca;3;\n2026-02-11;Ca;2;\n")
        self.append(settings, "2026-02-12;Dividends;Ca;7;Eq;;T;\n")  # a result of T instead
        snapshot = LedgerStore(settings).load()
        assert [(r.base_movement, r.acc_debit, r.acc_credit, r.amount) for r in snapshot.interest_rows.itertuples()] == [
            ("Interest on Money (L)", "Eq", "Bk", "1.00"), ("Interest on Money (G)", "Ca", "Eq", "3.00"),
            ("Interest on Money (G)", "Ca", "Eq", "2.00"),
        ]
        context = build_investments(snapshot)
        assert [(a["name"], a["gains"], a["losses"], a["net"]) for a in context["interest_accounts"]] == [
            ("Ca", 5, 0, 5), ("Bk", 0, 1, -1),
        ]
        assert [(p["month"], p["value"]) for p in context["interest_series"]] == [
            ("2025-12", 0), ("2026-01", 3), ("2026-02", 1),
        ]
        # Capital gains of 5, the 7 of T and the net interest of 4
        assert context["result_totals"]["interest"] == 4
        assert context["result_totals"]["total"] == 16
        assert [p["value"] for p in context["result_series"]] == [0, 8, 16]
        r = client.get("/investments")
        assert "<h2>Interest on money <small>all time</small></h2>" in r.text
        assert 'data-sortable="interest-accounts"' in r.text and 'data-sortable="interest-over-time"' in r.text
        # Listed with the other investment movements, and deleted from their CSV
        assert 'hx-post="/investments/interest/1/delete"' in r.text
        fp = interest_fingerprint(LedgerStore(settings).load().interest.iloc[1])
        assert client.post("/investments/interest/1/delete", data={"fingerprint": fp}).status_code == 200
        assert client.delete("/api/investments/interest/1").status_code == 200
        assert settings.interest_path.read_text() == "move_date;acc_debit;amount;comment;id\n2026-02-10;Bk;-1.00;;M-1\n"

    @pytest.mark.parametrize(
        ("line", "message"),
        [
            ("2026-01-10;Br;3;", "cannot be of an investment account: Br"),
            ("2026-01-10;Xx;3;", "Unknown account of the interest on money: 'Xx'"),
            ("2026-01-10;Ca;x;", "Invalid amount of the interest on money"),
            ("2026-01-10;Ca;3;", "'Interest on Money (G)' is required for the interest on money"),
        ],
    )
    def test_invalid_interest(self, settings, line, message):
        path = settings.base_movements_path
        if "required" not in message:
            path.write_text(path.read_text().replace("Dividends: d\n", "Dividends: d\n        Interest on Money (G): d\n", 1))
        settings.interest_path.write_text("move_date;acc_debit;amount;comment\n" + line + "\n")
        with pytest.raises(LedgerDataError, match=re.escape(message)):
            LedgerStore(settings).load()

    def test_invested_and_result_over_time(self, settings, client):
        snapshot = LedgerStore(settings).load()
        charts = investment_charts(snapshot)
        # T starts at 100 on the first ledger date, then gains 5 in 2026-01
        assert [(p["month"], p["value"], p["change"]) for p in charts["invested_series"]] == [
            ("2025-12", 100.0, 100.0), ("2026-01", 105.0, 5.0)
        ]
        assert [(p["month"], p["value"]) for p in charts["result_series"]] == [("2025-12", 0.0), ("2026-01", 5.0)]
        # The real total invested, at the CPI of each month end
        assert charts["invested_chart"]["real_date"] == "2026-01-31"
        assert [p["real"] for p in charts["invested_series"]] == [pytest.approx(103.0), pytest.approx(105.0)]
        assert not charts["result_chart"]["real_path"]
        assert snapshot.investment_accounts(month="2025-12")[0].investments["T"]["total_amount"] == 100
        r = client.get("/investments")
        assert "Total invested <small>all time, at each month end</small>" in r.text
        assert "Total result <small>all time, at each month end</small>" in r.text
        assert 'data-sortable="invested-over-time"' in r.text and 'data-sortable="result-over-time"' in r.text

    def test_internal_investment_transfers_too(self, settings, client):
        r = client.get("/form/invest-bases", params={"move_type": "transfer"}, headers=HTMX)
        assert "<option selected>Investment - Bought</option>" in r.text
        assert "Adjust" not in r.text  # no INVESTMENT side: never by hand
        data = {"move_date": "2026-02-01", "base_movement": "Investment - Bought",
                "acc_debit": "Br", "acc_credit": "Ca", "amount": "3", "investment": "T", "quantity": "1"}
        r = client.post("/investments/movements", data=data, headers=HTMX)
        assert "Added Investment - Bought of T: $3.00" in r.text
        r = client.post("/movements", data={**data, "move_type": "transfer"}, headers=HTMX)
        assert "added from the Investments tab" in r.text
        with pytest.raises(ValueError, match="'Adjust' is not an investment base movement"):
            LedgerStore(settings).add("2026-02-01", "Adjust", "Bk", "Ca", "1", invest=True)
        assert list(investment_rows(settings).base_movement) == ["Capital gains", "Investment - Bought"]

    def holding(self, settings, name="T"):
        br = LedgerStore(settings).load().investment_accounts()[0]
        h = br.investments[name]
        return h["investment"].type, h["total_amount"], h["total_quantity"]

    def test_movements_update_the_investments(self, settings):
        assert self.holding(settings) == (InvestmentType.STOCK, 105, 10)  # capital gain of 5
        self.append(settings, "2026-01-06;Investment - Bought;Br;\"1,000\";Ca;2.50;T;\n"
                              "2026-01-07;Investment - Sold;Ca;300;Br;4;T;\n"
                              "2026-01-08;Broker fees;Eq;1;Ca;;T;\n"  # no investment account: no change
                              "2026-01-09;Investment - Bought;Br;50;Ca;1;NEW;\n")
        assert self.holding(settings) == (InvestmentType.STOCK, 805, 8.5)
        assert self.holding(settings, "NEW") == (None, 50, 1)

    @pytest.mark.parametrize(
        ("row", "message"),
        [
            ("2026-01-06;Investment - Sold;Ca;1;Br;11;T;", r"quantity of 'T' .* cannot be negative"),
            ("2026-01-06;Investment - Bought;Br;1;Ca;;T;", "must have a quantity"),
            ("2026-01-06;Investment - Bought;Br;1;Ca;1;;", "must name their investment"),
            ("2026-01-06;Capital gains;Br;1;Eq;1;T;", "cannot have a quantity"),
            ("2026-01-06;Investment - Bought;Br;1;Ca;x;T;", "Invalid quantity"),
            ("2026-01-06;Broker fees;Eq;1;Br;;T;", "credit account of 'Broker fees' cannot be an investment account"),
            ("2026-01-06;Dividends;Br;1;Eq;;T;", "debit account of 'Dividends' cannot be an investment account"),
            ("2026-01-06;Food;Eq;1;Ca;;;", "can only have movements of the investment base movements"),
        ],
    )
    def test_invalid_rows(self, settings, row, message):
        self.append(settings, row + "\n")
        with pytest.raises(LedgerDataError, match=message):
            LedgerStore(settings).load()

    def test_form_fields_follow_the_base_movement(self, client):
        f = client.get("/form/invest-fields", params={"base_movement": "Investment - Bought"}).text
        assert re.search(r'name="quantity"[^>]*\s+required', f) and 'name="investment_type"' not in f
        investment = f.split('<select name="investment"')[1].split("</select>")[0]
        assert investment.startswith(" required>") and '<optgroup label="Br">' in investment
        assert "<option >T</option>" in investment  # known investments to pick from
        assert "Add it in <strong>New investment</strong> first" in f
        f = client.get("/form/invest-fields", params={"base_movement": "Broker fees"}).text
        assert 'name="quantity"' not in f and 'name="investment_type"' not in f
        assert "Investment <em class=\"optional\">(optional)</em>" in f
        assert '<option value="">-</option>' in f

    def test_adding_writes_the_details_and_new_investments(self, settings, client):
        data = {"move_date": "2026-02-01", "base_movement": "Investment - Bought",
                "acc_debit": "Br", "acc_credit": "Ca", "amount": "30", "investment": "AL30", "quantity": "1234.5"}
        r = client.post("/investments/movements", data=data)
        assert "&#39;AL30&#39; is not an investment of Br: add it as a new investment first" in r.text
        new = client.post("/investments/new", data={"account": "Br", "name": "AL30", "investment_type": "public_bond"})
        assert "Added AL30 to Br" in new.text and new.headers["HX-Trigger"] == "investment-added"
        r = client.post("/investments/movements", data=data)
        assert "Added Investment - Bought of AL30" in r.text
        path = settings.investment_movements_path
        assert path.read_text().splitlines()[-1] == "2026-02-01;Investment - Bought;Br;30.00;Ca;1,234.50;AL30;;I-2"
        assert "  AL30: public_bond\n" in settings.investments_path.read_text()
        assert self.holding(settings, "AL30") == (InvestmentType.PUBLIC_BOND, 30, 1234.5)
        # Selling more than is held changes nothing
        before = path.read_text()
        sell = {**data, "base_movement": "Investment - Sold", "acc_debit": "Ca", "acc_credit": "Br",
                "quantity": "2,000"}
        assert "cannot be negative" in client.post("/investments/movements", data=sell).text
        assert path.read_text() == before
        body = {**data, "investment": "T", "quantity": 1, "amount": 7}
        assert client.post("/api/investments/movements", json=body).status_code == 201
        assert client.get("/api/investments/movements").json()[0]["quantity"] == "1.00"

    def test_adding_creates_the_csv(self, settings):
        settings.investment_movements_path.unlink()
        LedgerStore(settings).add("2026-02-01", "Dividends", "Ca", "Eq", "4", "x", invest=True, investment="T")
        assert settings.investment_movements_path.read_text() == (
            INVESTMENT_HEADER.replace("\n", ";id\n") + "2026-02-01;Dividends;Ca;4.00;Eq;;T;x;I-1\n"
        )

    def test_investments_tab_shows_held_and_untyped_ones(self, settings, client):
        self.append(settings, "2026-01-09;Investment - Bought;Br;50;Ca;1;NEW;\n"
                              "2026-01-10;Investment - Sold;Ca;105;Br;10;T;\n")
        r = client.get("/investments")
        assert "⚠ 1 investment has no type: NEW." in r.text and "<td>No type</td>" in r.text
        held, closed = r.text.split("<h2>No longer held <small>1</small></h2>")
        assert "<strong>T</strong>" not in held and "<strong>T</strong>" in closed.split("</table>")[0]
        # The movements list shows the investment and the quantity
        moves = r.text.split("<h2>Investment movements")[1]
        assert '<td class="">NEW</td>\n          <td class="num ">1</td>' in moves

    def test_results_are_totalled_per_investment_whatever_the_accounts(self, settings, client):
        self.append(settings, "2026-01-06;Dividends;Ca;10;Eq;;T;\n"
                              "2026-01-07;Dividends;Bk;2.5;Eq;;T;\n"
                              "2026-01-08;Broker fees;Eq;1;Ca;;T;\n"
                              "2026-01-09;Capital losses;Eq;2;Br;;T;\n"
                              "2026-01-10;Investment - Sold;Ca;103;Br;10;T;\n"  # all of it
                              "2026-01-10;Broker fees;Eq;4;Ca;;;\n"  # names no investment
                              "2026-01-11;Dividends;Ca;7;Eq;;GHOST;\n")
        unattributed = []
        br = LedgerStore(settings).load().investment_accounts(unattributed)[0]
        assert br.results == {"T": {"Capital gains": 5.0, "Dividends": 12.5, "Broker fees": 1.0, "Capital losses": 2.0}}
        assert br.investments["T"]["total_amount"] == 0  # 100 + 5 - 2 - 103
        assert [(u["investment"], u["amount"]) for u in unattributed] == [("GHOST", 7.0)]
        r = client.get("/investments")
        assert card_labels(r.text)[-4:] == ["Net capital gains", "Dividends", "Broker fees", "Total result"]
        closed = r.text.split("<h2>No longer held")[1].split("</table>")[0]
        assert ">Net capital gains</th>" in closed and ">Total result</th>" in closed
        # Sold, but its capital gains (5 - 2) and total result (3 + 12.50 - 1) stay
        for value in ("3.0\">$3.00", "12.5\">$12.50", "1.0\">$1.00", "14.5\">$14.50"):
            assert f'data-sort="{value}</td>' in closed
        assert "⚠ 1 movement names an investment of no investment account:\n      GHOST." in r.text

    def test_free_sides_never_take_investment_accounts(self, settings, client):
        f = client.get("/form/invest-fields", params={"base_movement": "Investment - Bought"}).text
        credit = f.split('name="acc_credit"')[1].split("</select>")[0]
        assert "<option >Ca</option>" in credit and "<option >Br</option>" not in credit
        with pytest.raises(ValueError, match="credit account of 'Broker fees' cannot be an investment account: Br"):
            LedgerStore(settings).add("2026-02-01", "Broker fees", "Eq", "Br", "1", invest=True, investment="T")

    def test_no_form_without_invest_base_movements(self, settings, client):
        path = settings.base_movements_path
        path.write_text(path.read_text().replace(INVEST_YAML, ""))
        settings.investment_movements_path.unlink()
        assert 'id="invest-form"' not in client.get("/investments").text


# The internal base movement of the Credit Cards tab; appended to the `internal` section of INITIAL_YAML
CARD_PAYMENT_YAML = "  transfer:\n    Credit card payment:\n      description: d\n      debit: CREDIT CARD\n"
CREDIT_CARDS_YAML = """\
Vi:
  long_name: Visa (L)
  description: Visa card
  initial_amount: 100
MM:
  description: Mastercard
  initial_amount: 0
"""


class TestCreditCards:
    @pytest.fixture
    def settings(self, settings, tmp_path):
        path = tmp_path / "credit_cards.yaml"
        path.write_text(CREDIT_CARDS_YAML, encoding="utf-8")
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write("2026-01-05;Food;Eq;30;Vi;\n2026-02-05;Move;Vi;50;Bk;\n2026-02-06;Food;Eq;20;MM;\n")
        return replace(settings, credit_cards_path=path)

    @pytest.fixture
    def client(self, settings):
        return TestClient(create_app(settings))

    def test_are_liquid_liabilities(self, settings):
        accounts = LedgerStore(settings).load().accounts
        vi = accounts["Vi"]
        assert isinstance(vi, CreditCardAccount) and vi.acc_type.value == "liability" and vi.is_liquid
        assert vi.initial_amount == 100 and list(accounts)[-2:] == ["Vi", "MM"]
        assert LedgerStore(settings).load().ledger().balances()["Vi"] == 80

    @pytest.mark.parametrize(
        ("old", "new", "message"),
        [
            ("  initial_amount: 0\n", "  initial_amount: 0\n  is_liquid: false\n", "always liquid"),
            ("MM:\n", "Ca:\n", "Duplicated account name: 'Ca'"),
            ("  initial_amount: 0\n", "", "'MM' must have an initial_amount"),
        ],
    )
    def test_invalid(self, settings, old, new, message):
        settings.credit_cards_path.write_text(CREDIT_CARDS_YAML.replace(old, new), encoding="utf-8")
        with pytest.raises(LedgerDataError, match=message):
            LedgerStore(settings).load()

    def test_cannot_be_debt_accounts(self, settings, tmp_path):
        creditors = tmp_path / "creditors.yaml"
        creditors.write_text("Vi:\n  people:\n    Dad: 1\n", encoding="utf-8")
        with pytest.raises(LedgerDataError, match="cannot be investment or debt accounts"):
            LedgerStore(replace(settings, creditors_path=creditors)).load()

    @pytest.fixture
    def paying(self, settings):
        """With the internal card payment base movement, debiting any credit card."""
        path = settings.base_movements_path
        path.write_text(path.read_text().replace(INITIAL_YAML, INITIAL_YAML + CARD_PAYMENT_YAML, 1))
        return settings

    def test_card_payments_are_added_from_their_tab(self, paying):
        client = TestClient(create_app(paying))
        pay = "Credit card payment"
        assert LedgerStore(paying).load().card_payment_base_movements().keys() == {pay}
        with pytest.raises(ValueError, match="'Credit card payment' are added from the Credit Cards tab"):
            LedgerStore(paying).add("2026-02-07", pay, "Vi", "Bk", "10")
        assert pay not in client.get("/form/bases", params={"move_type": "transfer"}).text
        r = client.get("/credit-cards", params={"month": ""})
        form = r.text.split('id="card-payment-form"')[1].split("</form>")[0]
        # The base movement is fixed, and the debit is any credit card
        assert f'<input type="hidden" name="base_movement" value="{pay}">' in form
        debit = form.split('name="acc_debit"')[1].split("</select>")[0]
        assert "<option >Vi</option>" in debit and "<option >MM</option>" in debit and "Bk" not in debit
        assert 'hx-trigger="credit-cards-changed from:body"' in r.text
        r = client.post("/credit-cards/payments", data={
            "move_date": "2026-02-07", "base_movement": pay, "acc_debit": "Vi", "acc_credit": "Bk", "amount": "10",
        })
        assert "Paid Vi: $10.00" in r.text and r.headers["HX-Trigger"] == "credit-cards-changed"
        assert rows(paying).iloc[-1][["base_movement", "acc_debit", "acc_credit"]].tolist() == [pay, "Vi", "Bk"]
        r = client.post("/credit-cards/payments", data={
            "move_date": "2026-02-07", "base_movement": pay, "acc_debit": "Bk", "acc_credit": "Ca", "amount": "10",
        })
        assert "must be a credit card account" in r.text
        body = {"move_date": "2026-02-07", "base_movement": pay, "acc_debit": "Vi", "acc_credit": "Bk", "amount": 1}
        assert client.post("/api/credit-cards/payments", json=body).status_code == 201
        assert client.post("/api/movements", json=body).status_code == 422
        # The Ledger tab links to them in the Credit Cards tab
        LedgerStore(paying).freeze_ids()
        assert 'href="/credit-cards?month=2026-02#movement-L-8" title="Added, edited and deleted from the Credit Cards tab"' in client.get("/").text

    def test_edit_and_delete_card_payment(self, paying):
        client = TestClient(create_app(paying))
        pay = "Credit card payment"
        LedgerStore(paying).add("2026-02-07", pay, "Vi", "Bk", "10", card_payment=True)
        index = len(rows(paying)) - 1
        page = client.get("/credit-cards", params={"month": ""}).text
        assert page.count('hx-get="/credit-cards/payments/') == 1  # only the card payment
        assert f'hx-post="/credit-cards/payments/{index}/delete"' in page
        fp = row_fingerprint(rows(paying).iloc[index])
        r = client.get(f"/credit-cards/payments/{index}/edit", params={"fingerprint": fp})
        assert "Edit card payment" in r.text and 'value="10.00"' in r.text and "<option selected>Vi</option>" in r.text
        r = client.post(f"/credit-cards/payments/{index}/edit", data={
            "fingerprint": fp, "move_date": "2026-02-07", "base_movement": pay, "acc_debit": "MM",
            "acc_credit": "Bk", "amount": "15",
        })
        assert f"Updated L-{index + 1} payment of MM: $15.00" in r.text and r.headers["HX-Trigger"] == "credit-cards-changed"
        assert rows(paying).iloc[index][["acc_debit", "amount"]].tolist() == ["MM", "15.00"]
        fp = row_fingerprint(rows(paying).iloc[index])
        assert client.post(f"/movements/{index}/delete", data={"fingerprint": fp}).status_code == 422
        r = client.post(f"/credit-cards/payments/{index}/delete", data={"fingerprint": fp})
        assert r.status_code == 200 and r.headers["HX-Trigger"] == "credit-cards-changed"
        assert len(rows(paying)) == index

    def test_card_filter(self, paying, settings):
        data = build_credit_cards(LedgerStore(settings).load(), "", "MM")
        assert data["card"] == "MM" and data["movements"]
        assert all("MM" in (m["debit"], m["credit"]) for m in data["movements"])
        assert len(build_credit_cards(LedgerStore(settings).load(), "", "Nope")["movements"]) > len(data["movements"])
        r = TestClient(create_app(settings)).get("/credit-cards", params={"month": "", "card": "Vi"})
        assert '<form id="card-filter"' in r.text and "<option selected>Vi</option>" in r.text

    def test_credit_cards_tab_without_card_payment_base_movements(self, client):
        assert "The Credit Cards tab needs this base movement" in client.get("/credit-cards").text

    def test_credit_cards_tab(self, client):
        r = client.get("/credit-cards", params={"month": ""})
        assert r.status_code == 200
        assert 'href="/credit-cards" aria-current="page"' in r.text
        assert card_labels(r.text) == ["Owed", "Charged", "Paid"]
        cards = r.text.split('<div class="cards">')[1].split("</div>\n  </div>")[0]
        # Owed 80 + 20; charged 30 + 20 and paid 50, without the initial 100
        assert "$100.00" in cards and "$50.00" in cards
        assert '<span class="bar-name">Vi</span>' in r.text
        assert "<strong>Vi</strong></td>\n        <td class=\"muted\">Visa card</td>" in r.text
        # The Accounts tab marks them
        assert ">Credit card</td>" in client.get("/accounts").text

    def test_month_filter(self, client):
        r = client.get("/credit-cards", params={"month": "2026-01"})
        assert '<option value="2026-01" selected>' in r.text and "as of end of 2026-01" in r.text
        cards = r.text.split('<div class="cards">')[1].split("</div>\n  </div>")[0]
        assert "$130.00" in cards and "$30.00" in cards and "$0.00" in cards
        r = client.get("/credit-cards", params={"month": "2026-02"}, headers=HTMX)
        assert "<html" not in r.text and 'data-sort="20.0">$20.00</td>' in r.text

    def test_movements(self, client):
        r = client.get("/credit-cards", params={"month": ""})
        movements = r.text.split('data-sortable="credit-card-movements"')[1].split("</table>")[0]
        assert movement_ids(movements)[-1].startswith("A-")  # the ids of the Ledger tab, even filtered
        assert set(movement_ids(movements)) <= set(movement_ids(client.get("/", params={"initial": True}).text))
        # With the initial ones, but not the ones of other accounts
        assert "<h2>Movements <small>4, all time</small></h2>" in r.text
        assert movements.count("<tr") == 5 and "Initial (L)" in movements and "Salary" not in movements
        r = client.get("/credit-cards", params={"month": "2026-01"}, headers=HTMX)
        assert "<h2>Movements <small>2, in 2026-01</small></h2>" in r.text  # with the initial one

    def test_net_owed_chart(self, settings, client):
        cards = build_credit_cards(LedgerStore(settings).load())
        # Nothing in December (the ledger starts with Bk); Vi's initial 100 and 30 more in January;
        # 50 paid off Vi and 20 charged to MM in February
        assert [(p["month"], p["value"], p["change"]) for p in cards["owed_amounts"]] == [
            ("2025-12", 0, 0), ("2026-01", 130, 130), ("2026-02", 100, -30),
        ]
        reals = [p["real"] for p in cards["owed_amounts"]]
        assert reals[0] == 0 and reals[1] == pytest.approx(130)  # at the prices of 2026-01-31
        assert reals[2] is None  # no CPI after 2026-01
        r = client.get("/credit-cards", params={"month": ""})
        panel = r.text.split("<h2>Net owed <small>")[1].split("</section>")[0]
        assert '--line: var(--series-2)' in panel and "Real <span class=\"muted\">(prices of 2026-01-31)" in panel
        assert 'data-sortable="owed"' in panel
        charged = r.text.split("<h2>Charged per month")[1].split("</section>")[0]
        ratio = r.text.split("<h2>Owed over equity")[1].split("</section>")[0]
        assert "--line: var(--series-8)" in charged and "--line: var(--series-liability-nested)" in ratio

    def test_charged_per_month_chart(self, settings, client):
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write("2026-02-07;Food;Eq;5;Vi;\n")
        series = build_credit_cards(LedgerStore(settings).load())["charged_series"]
        # Only the charges (credits): not the initial 100 nor the payment of 50
        assert [(p["month"], p["value"], p["count"]) for p in series] == [
            ("2025-12", 0.0, 0), ("2026-01", 30.0, 1), ("2026-02", 25.0, 2)
        ]
        r = client.get("/credit-cards", params={"month": "2026-01"})
        assert "Charged per month <small>up to 2026-01</small>" in r.text
        chart = r.text.split('class="line-chart columns"')[1].split('<div class="x-axis"')[0]
        assert chart.count('class="column"') == 2 and "height: 100.0%" in chart
        assert '"change_text": "1 charge"' in r.text and 'data-sortable="charged"' in r.text

    def test_owed_over_equity_chart(self, settings, client):
        series = build_credit_cards(LedgerStore(settings).load())["owed_series"]
        # Vi starts at 100 on the first ledger date; equity counts every account
        assert [(p["month"], p["amount"]) for p in series] == [("2025-12", 0.0), ("2026-01", 130.0), ("2026-02", 100.0)]
        last = series[-1]
        assert last["value"] == pytest.approx(100 / last["equity"])
        r = client.get("/credit-cards", params={"month": "2026-01"})
        assert "Owed over equity <small>up to 2026-01, at each month end</small>" in r.text
        assert 'data-sortable="owed-ratio"' in r.text and "2026-02</td>" not in r.text.split("Owed over equity")[1]
        # The Accounts tab still has its own ratio panels
        charts = client.get("/accounts", params={"month": ""}).text
        assert "Debt over equity <small>" in charts and "Net liquidity over equity <small>" in charts

    def test_negative_balance_warning(self, settings, client):
        assert 'class="msg warn alert"' not in client.get("/credit-cards", params={"month": ""}).text
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write("2026-02-07;Move;MM;25.5;Bk;\n")
        r = client.get("/credit-cards", params={"month": ""})
        alert = r.text.split('class="msg warn alert"')[1].split("</details>")[0]
        assert "1 credit card has a negative" in alert
        assert "<strong>MM</strong>" in alert and "-$5.50" in alert and "Vi" not in alert
        assert "⚠ -$5.50</td>" in r.text
        # Not before it went negative
        assert 'class="msg warn alert"' not in client.get("/credit-cards", params={"month": "2026-01"}).text

    def test_without_credit_cards(self, settings):
        settings.ledger_path.write_text("".join(settings.ledger_path.read_text().splitlines(True)[:-3]))
        r = TestClient(create_app(replace(settings, credit_cards_path=None))).get("/credit-cards")
        assert "No credit card accounts yet" in r.text


def test_missing_files_are_created(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings.from_env()
    created = settings.create_missing_files()
    assert created == list(settings.expected_files())
    assert all(p.is_file() for p in created) and (tmp_path / "info" / "movements").is_dir()
    assert settings.ledger_path.read_text() == "move_date;base_movement;acc_debit;amount;acc_credit;comment;id\n"
    assert settings.accounts_path.read_text() == ""
    settings.accounts_path.write_text("asset: {}\n")
    assert settings.create_missing_files() == []  # nothing missing, nothing touched
    assert settings.accounts_path.read_text() == "asset: {}\n"


def test_startup_aborts_with_missing_files(tmp_path, monkeypatch, capsys):
    from pledger.web import __main__

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["pledger-web"])
    monkeypatch.setattr(__main__.uvicorn, "run", lambda *a, **k: pytest.fail("pledger started"))
    with pytest.raises(SystemExit) as e:
        __main__.main()
    assert "info/accounts.yaml" in str(e.value) and "were created empty" in str(e.value)
    # They were all created, so it starts now
    started = []
    monkeypatch.setattr(__main__.uvicorn, "run", lambda *a, **k: started.append(a))
    __main__.main()
    assert started


class TestEditMovements:
    def test_edit_replaces_only_that_line_and_keeps_its_id(self, settings):
        store = LedgerStore(settings)
        store.freeze_ids()
        path = settings.ledger_path
        before = path.read_text().splitlines()
        store.edit(2, row_fingerprint(store.load().rows.iloc[2]), "2026-01-05", "Food", "Eq", "Ca", "60", "a; b")
        after = path.read_text().splitlines()
        assert after[:3] == before[:3] and after[4:] == before[4:]
        assert after[3] == "2026-01-05;Food;Eq;60.00;Ca;a, b;L-3"

    def test_edit_with_stale_fingerprint_is_rejected(self, settings):
        with pytest.raises(StaleRowError):
            LedgerStore(settings).edit(2, "2000-01-01|Food|1", "2026-01-05", "Food", "Eq", "Ca", "60")
        with pytest.raises(StaleRowError):
            LedgerStore(settings).edit(9, None, "2026-01-05", "Food", "Eq", "Ca", "60")

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (("2026-13-01", "Food", "Eq", "Ca", "1"), "Invalid date"),
            (("2026-01-03", "Food", "Eq", "Ca", "-1"), "cannot be negative"),
            (("2026-01-03", "Food", "Ca", "Bk", "1"), "Movement type must match"),
        ],
    )
    def test_edit_rejects_invalid_movements(self, settings, args, message):
        store = LedgerStore(settings)
        before = settings.ledger_path.read_text()
        with pytest.raises(ValueError, match=message):
            store.edit(2, row_fingerprint(store.load().rows.iloc[2]), *args)
        assert settings.ledger_path.read_text() == before

    def test_edit_breaking_the_balances_is_rejected(self, settings):
        path = Path(settings.accounts_path)
        path.write_text(path.read_text().replace("    initial_amount: 0.0\n", "    initial_amount: 0.0\n    checks: [net_positive]\n", 1))
        store = LedgerStore(settings)
        with pytest.raises(ValueError):
            store.edit(2, row_fingerprint(store.load().rows.iloc[2]), "2026-01-03", "Food", "Eq", "Ca", "500")

    def test_ledger_rows_have_an_edit_button(self, client):
        r = client.get("/")
        assert r.text.count('hx-get="/movements/') == 4
        assert 'hx-get="/movements/2/edit"' in r.text and "&#9998;" in r.text

    def test_edit_form_is_filled_with_the_row(self, client, settings):
        fp = row_fingerprint(rows(settings).iloc[2])
        r = client.get("/movements/2/edit", params={"fingerprint": fp}, headers=HTMX)
        assert r.status_code == 200
        assert "Edit movement" in r.text and "New movement" not in r.text
        assert 'hx-post="/movements/2/edit"' in r.text and 'hx-get="/form/movement"' in r.text
        assert 'value="2026-01-03"' in r.text and 'value="50.25"' in r.text
        assert "<option selected>Food</option>" in r.text
        assert client.get("/movements/2/edit", params={"fingerprint": "x"}).status_code == 409

    def test_edit_saves_and_goes_back_to_a_new_movement(self, client, settings):
        fp = row_fingerprint(rows(settings).iloc[2])
        form = {
            "fingerprint": fp, "move_date": "2026-01-03", "move_type": "loss", "base_movement": "Food",
            "acc_debit": "Eq", "acc_credit": "Ca", "amount": "60", "comment": "edited",
        }
        r = client.post("/movements/2/edit", data=form, headers=HTMX)
        assert r.status_code == 200 and r.headers["HX-Trigger"] == "ledger-changed"
        assert "Updated" in r.text and "New movement" in r.text
        assert rows(settings).iloc[2]["amount"] == "60.00" and rows(settings).iloc[2]["comment"] == "edited"
        assert len(rows(settings)) == 4
        # The old fingerprint is stale now
        assert client.post("/movements/2/edit", data=form, headers=HTMX).status_code == 409

    def test_edit_errors_keep_editing(self, client, settings):
        fp = row_fingerprint(rows(settings).iloc[2])
        form = {"fingerprint": fp, "move_date": "2026-01-03", "base_movement": "Food",
                "acc_debit": "Eq", "acc_credit": "Eq", "amount": "60"}
        r = client.post("/movements/2/edit", data=form, headers=HTMX)
        assert 'class="msg error"' in r.text and "Edit movement" in r.text
        assert rows(settings).iloc[2]["amount"] == "50.25"

    def test_cancel_gives_a_new_movement_form(self, client):
        r = client.get("/form/movement", headers=HTMX)
        assert "New movement" in r.text and 'hx-post="/movements"' in r.text

    def test_api(self, client, settings):
        fp = row_fingerprint(rows(settings).iloc[2])
        body = {"move_date": "2026-01-03", "base_movement": "Food", "acc_debit": "Eq", "acc_credit": "Ca", "amount": 70}
        assert client.put("/api/movements/2", params={"fingerprint": "x"}, json=body).status_code == 409
        assert client.put("/api/movements/2", params={"fingerprint": fp}, json=body).status_code == 200
        assert rows(settings).iloc[2]["amount"] == "70.00"


class TestIdLinks:
    def test_ledger_ids_link_like_their_circles(self, client, settings):
        LedgerStore(settings).freeze_ids()
        r = client.get("/", params={"initial": "true"})
        # The initial movement of Bk links to its account, by its circle and its ID
        assert r.text.count('href="/accounts#account-Bk"') == 2
        assert '<a class="id-link" href="/accounts#account-Bk"' in r.text
        # Rows deletable here have no link, but an anchor to come back to
        assert 'id="movement-L-3"' in r.text and 'class="id-link" href="/#' not in r.text

    def test_ledger_link(self):
        from pledger.web.views import ledger_link

        assert ledger_link("I-2", "2026-01-05") == "/?month=2026-01#movement-I-2"
        assert ledger_link("A-1", "2025-12-31", initial=True) == "/?month=2025-12&initial=true#movement-A-1"


class TestSaveAndQuit:
    def test_saves_the_ids_and_stops_the_server(self, client, settings, monkeypatch):
        from pledger.web.routers import server

        stopped = []
        monkeypatch.setattr(server, "stop_server", lambda: stopped.append(True))
        assert "L-1" not in settings.ledger_path.read_text()
        r = client.post("/quit")
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert stopped == [True]
        assert "L-1" in settings.ledger_path.read_text()

    def test_button_in_every_page(self, client):
        assert 'id="quit"' in client.get("/").text and 'id="quit"' in client.get("/help").text


class TestToggles:
    def test_expand_all_buttons(self, client):
        accounts = client.get("/accounts").text
        assert 'data-expand-all="accounts" aria-expanded="true">Collapse all' in accounts
        results = client.get("/results").text
        assert 'data-expand-all="results" aria-expanded="false">Expand all' in results

    def test_result_by_type_over_the_operative_gains(self, settings):
        relative = build_results(LedgerStore(settings).load())["relative"]
        assert relative["name"] == "Operative"
        (op, gifts) = relative["statement"]
        assert op["gains"] == pytest.approx(1) and op["losses"] == pytest.approx(-50.25 / 1000.5)
        assert gifts["gains"] == pytest.approx(10 / 1000.5)
        assert relative["waterfall"]["y_ticks"][-1]["label"].endswith("%")

    def test_result_by_type_toggle(self, client):
        r = client.get("/results")
        assert 'name="results-relative"' in r.text and "% of Operative gains" in r.text
        assert '<div data-relative="true">' in r.text and "100.0%" in r.text


class TestLanguage:
    @pytest.mark.parametrize("path", ["/", "/accounts", "/currencies", "/results", "/work", "/investments", "/debt", "/credit-cards", "/help"])
    def test_every_tab_is_in_english(self, client, path):
        r = client.get(path)
        assert r.headers["Content-Language"] == "en"
        assert '<html lang="en">' in r.text and '<meta http-equiv="content-language" content="en">' in r.text
