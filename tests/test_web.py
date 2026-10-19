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

from pledger.models import DEBT, INVESTMENT
from pledger.web.app import create_app
from pledger.web.store import (
    LedgerDataError,
    LedgerStore,
    Settings,
    _insert_yaml_entry,
    row_fingerprint,
    work_fingerprint,
)
from pledger.web.templating import money, quantity
from pledger.web.views import build_charts, composition, equity_series, nice_ticks

HTMX = {"HX-Request": "true"}

# The internal base movements required to load the ledger
INITIAL_YAML = (
    "internal:\n  Init:\n    id: 3\n    long_name: Initial\n    description: d\n"
    "    base_movements:\n      gain:\n        Initial (G): Initial balance\n"
    "      loss:\n        Initial (L): Initial balance\n"
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
        assert "$960.25" in r.text  # profit card, without Bk's initial 100.50
        assert "Balances" not in r.text and "New account" not in r.text
        assert card_labels(r.text) == ["Gains", "Losses", "Profit"]

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

    def test_charts_balances_grouped_by_currency_or_liquidity(self, client):
        assert 'name="accounts-group"' not in client.get("/accounts").text  # only in Charts
        r = client.get("/charts")
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
        client.get("/charts", params={"month": "2026-02"})
        for path in ["/", "/accounts", "/charts"]:
            assert '<option value="2026-02" selected>' in client.get(path).text, path
        # An explicit "All time" is remembered too
        client.get("/accounts", params={"month": ""})
        assert '<option value="2026-02" selected>' not in client.get("/").text
        # Months without movements, or malformed ones, are ignored
        client.cookies.set("pledger_month", "2030-01")
        assert "as of end of" not in client.get("/accounts").text
        assert client.get("/", params={"month": "<x>"}).status_code == 200

    def test_visibility_toggle_marks_the_amounts(self, client):
        for path in ["/", "/accounts", "/charts", "/investments", "/debt"]:
            r = client.get(path)
            assert 'id="visibility"' in r.text and 'class="eye-closed"' in r.text, path
        r = client.get("/")
        assert '<span class="num money gain">$1,010.50</span>' in r.text  # gains card
        assert 'data-confirm-money="$10.00"' in r.text  # masked in the delete question too
        assert 'class="total money"' in client.get("/charts").text

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

    def test_setup(self, client):
        r = client.get("/setup")
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

    def test_setup_create_result_type(self, client, settings):
        data = {"name": "Tax", "long_name": "Taxes", "description": "Taxes paid"}
        r = client.post("/setup/result-types", data=data, headers=HTMX)
        assert "Added result type Tax" in r.text and ">Taxes</td>" in r.text
        r = client.post("/setup/result-types", data=data, headers=HTMX)
        assert 'class="msg error"' in r.text and "already exists" in r.text

    def test_help(self, client):
        r = client.get("/help")
        assert r.status_code == 200
        assert 'href="/help" aria-current="page"' in r.text
        assert "<h1>Pledger</h1>" in r.text and "help-logo" not in r.text
        # Section headings carry the same numbers as the index
        toc = r.text.split('<ol>')[1].split("</ol>")[0]
        titles = [li.split("</span> ")[1].split("</a>")[0] for li in toc.split("<li>")[1:]]
        assert len(titles) == 7
        for n, title in enumerate(titles, start=1):
            assert f'<span class="help-num">{n}.</span>' in r.text
            assert f"</span> {title}</h2>" in r.text

    @pytest.mark.parametrize(
        "path", ["/", "/accounts", "/investments", "/debt", "/charts", "/setup", "/help"]
    )
    def test_footer(self, client, path):
        text = client.get(path).text
        assert "&copy; 2026 Pledger by trOOnies" in text
        assert "Made with Claude Code" in text

    def test_tables_are_sortable(self, client):
        index, setup = client.get("/").text, client.get("/setup").text
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

    def test_setup_create_base_movement(self, client, settings):
        data = {
            "name": "Rent", "move_type": "loss", "description": "Rent", "credit": "Bk",
            "result_type": "Gifts",
        }
        r = client.post("/setup/base-movements", data=data, headers=HTMX)
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

    def test_charts(self, client):
        r = client.get("/charts")
        assert r.status_code == 200
        assert 'href="/charts" aria-current="page"' in r.text
        assert "Assets &amp; liabilities" in r.text and "Equity over time" in r.text
        assert "$901.00" in r.text and "$159.75" in r.text  # Bk and Ca balances
        assert "Salary" in r.text and "Food" in r.text
        assert "by result type" in r.text and "Result by type" in r.text
        assert "$1,060.75" in r.text  # final equity
        assert 'data-sortable="equity"' in r.text

    def test_charts_partial_for_month(self, client, settings):
        with settings.ledger_path.open("a") as f:
            f.write("2026-02-01;Food;Eq;100;Ca;\n")
        r = client.get("/charts", params={"month": "2026-02"}, headers=HTMX)
        assert "<html" not in r.text
        assert "as of end of 2026-02" in r.text
        assert "Salary" in r.text  # no gains in February, but still listed at zero
        assert "-$100.00" in r.text and "$960.75" in r.text


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
        charts = build_charts(LedgerStore(settings).load())
        assets = charts["groupings"]["liquidity"]["assets"][0]["children"]
        assert [r["name"] for r in assets] == ["Ca", "Bk"]  # accounts.yaml order
        assert [r["name"] for r in charts["groupings"]["currency"]["assets"]] == ["ARS", "USD"]
        assert [r["name"] for r in charts["groupings"]["liquidity"]["liabilities"]] == ["Illiquid"]
        losses = charts["losses"][0]["children"]
        assert [r["name"] for r in losses] == ["Food", "Card fee"]  # definition order, not size
        gains = charts["gains"]
        assert [r["name"] for r in gains] == ["Operative", "Gifts"]  # result type id order, no Initial
        gains = build_charts(LedgerStore(settings).load(), initial=True)["gains"]
        assert [r["name"] for r in gains] == ["Operative", "Gifts", "Initial"]

    def test_charts_list_everything_even_at_zero(self, settings):
        with settings.ledger_path.open("a") as f:
            f.write("2026-02-01;Food;Eq;100;Ca;\n")
        charts = build_charts(LedgerStore(settings).load(), "2026-02")
        # Only Food moved in February; every other bar is still there, at zero
        by_currency = charts["groupings"]["currency"]
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
        charts = build_charts(LedgerStore(settings).load())
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
        chart = build_charts(snapshot)["equity"]
        assert chart["points"][0]["x"] == 0 and chart["points"][-1]["x"] == 100
        assert chart["change"] == pytest.approx(960.25)  # the profit
        assert build_charts(snapshot, "2025-12")["equity"] is None
        assert build_charts(snapshot, "2025-12", initial=True)["equity"]["change"] == pytest.approx(100.5)

    def test_month_end_charts(self, settings):
        with settings.ledger_path.open("a") as f:
            f.write("2026-03-01;Card fee;Eq;60.75;Cc;\n")  # no movements in February
        snapshot = LedgerStore(settings).load()
        charts = build_charts(snapshot)
        # Liabilities over equity: none until Cc's fee
        assert [(p["month"], p["value"]) for p in charts["debt_series"]] == [
            ("2025-12", 0), ("2026-01", 0), ("2026-02", 0), ("2026-03", pytest.approx(60.75 / 1000)),
        ]
        # Liquid assets (Bk, Ca) minus liquid liabilities (none: Cc isn't liquid)
        assert charts["liquidity_series"][-1]["value"] == pytest.approx(1060.75 / 1000)
        assert charts["debt_ratio"]["y_ticks"][-1]["label"] == "8%"
        shares = charts["currency_shares"]
        assert [a["name"] for a in shares["areas"]] == ["ARS", "USD"]  # base first, at the bottom
        assert [p["rows"] for p in shares["points"]][:2] == [
            [["USD", "100.0%"], ["ARS", "0.0%"]], [["USD", "84.9%"], ["ARS", "15.1%"]],
        ]
        assert shares["areas"][1]["last"] == pytest.approx(901 / 1060.75)
        # Up to the month picked
        assert [p["month"] for p in build_charts(snapshot, "2026-01")["debt_series"]] == ["2025-12", "2026-01"]

    def test_ratios_without_positive_equity(self, settings):
        LedgerStore(settings).add("2026-02-01", "Card fee", "Eq", "Cc", "2000")
        charts = build_charts(LedgerStore(settings).load())
        assert charts["debt_series"][-1]["value"] is None
        assert "M" in charts["debt_ratio"]["path"] and charts["debt_ratio"]["last"]["month"] == "2026-01"

    def test_month_end_charts_render(self, client):
        r = client.get("/charts")
        for title in ("Debt over equity", "Net liquidity over equity", "Currencies over time"):
            assert f"<h2>{title} <small>all time, " in r.text
        assert 'class="line-chart stacked"' in r.text and "var(--series-2)" in r.text
        assert 'data-sortable="currencies"' in r.text


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
            "is_liquid": True, "initial_amount": 100.5, "initial_date": "2025-12-31",
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
        assert "Initial (G)" not in client.get("/charts").text

    def test_can_be_shown_but_not_deleted(self, client):
        movements = client.get("/api/movements", params={"initial": True}).json()
        assert movements[-1]["base"] == "Initial (G)"  # the oldest
        r = client.get("/", params={"initial": True})
        assert 'name="initial" value="true" checked>' in r.text
        assert r.text.count('hx-post="/movements/') == 4  # only the CSV rows
        assert "$1,060.75" in r.text  # the profit, with Bk's initial 100.50
        assert ">Initial<" in client.get("/charts", params={"initial": True}).text

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
        assert 'title="Any investment account">Investment<' in client.get("/setup").text

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
        with settings.base_movements_path.open("a") as f:
            f.write("  Borrow:\n    description: Borrow money\n    credit: DEBT\n")
        return replace(settings, creditors_path=path)

    @pytest.fixture
    def client(self, settings):
        return TestClient(create_app(settings))

    def test_add_movement(self, settings):
        store = LedgerStore(settings)
        with pytest.raises(ValueError, match="Credit account must be a debt account: Cc is not"):
            store.add("2026-02-01", "Borrow", "Ca", "Cc", "1")
        store.add("2026-02-01", "Borrow", "Ca", "De", "1")
        assert rows(settings).iloc[-1]["acc_credit"] == "De"

    def test_form_offers_only_debt_accounts(self, client):
        r = client.get("/form/accounts", params={"base_movement": "Borrow"})
        debit, credit = r.text.split("<span>Credit")
        assert "(debt account)" in credit and "<option >De</option>" in credit
        assert "<option >Cc</option>" not in credit and "<option >Cc</option>" in debit

    def test_add_base_movement(self, settings, client):
        LedgerStore(settings).add_base_movement("Repay", "transfer", "d", debit=DEBT)
        bases = {b["name"]: b for b in client.get("/api/base-movements").json()}
        assert (bases["Borrow"]["debit"], bases["Borrow"]["credit"]) == (None, "DEBT")
        assert (bases["Repay"]["debit"], bases["Repay"]["credit"]) == ("DEBT", None)
        r = client.get("/form/base-sides", params={"move_type": "transfer"})
        assert '<option value="DEBT" >Any debt account</option>' in r.text
        assert 'title="Any debt account">Debt<' in client.get("/setup").text

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
            for row in r.text.split("<tbody>", 1)[1].split("<tr>")[1:]
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
        assert card_labels(r.text) == ["Invested", "Investments", "Investment accounts"]
        assert '<span class="num money ">$500.00</span>' in r.text
        assert 'data-sortable="investments"' in r.text
        assert "<td>Public bond</td>" in r.text and "<td>ETF</td>" in r.text
        assert ">1,000</td>" in r.text and ">1.5</td>" in r.text and ">0.25</td>" in r.text
        assert ">60.0%</td>" in r.text  # T in Br: 300 of 500

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
        client.get("/charts", params={"month": "2025-12"})
        assert '<option value="2025-12" selected>' in client.get("/debt").text
        r = client.get("/debt", params={"month": "2026-02"}, headers=HTMX)
        assert "<html" not in r.text and '<div class="view">' in r.text and "$200.50" in r.text

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
        owed = r.text.split("Owed to us <span")[1].split("<h2>People</h2>")[0]
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
Month;Company;Position;Gross salary;Net salary
2025-10;Acme;Acme Jr;1000.00;800.00
2025-11;Acme;Acme Sr;1500.50;1200.00
2025-12;;;;
2026-01;Globex;GX Dev;2000.00;1600.00"""


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
            ("2026-02;Globex;GX Dev;;", "both a gross and a net salary"),
            ("2026-02;Globex;GX Dev;1;2", "over the gross"),
        ],
    )
    def test_data_errors(self, store, settings, line, error):
        settings.work_path.write_text(WORK_CSV + "\n" + line, encoding="utf-8")
        with pytest.raises(LedgerDataError, match=error):
            store.load_work()

    def test_missing_columns(self, store, settings):
        settings.work_path.write_text("Month;Company\n2026-01;;\n", encoding="utf-8")
        with pytest.raises(LedgerDataError, match="missing the columns"):
            store.load_work()

    def test_add_after_the_last_month_keeps_the_file(self, store, settings):
        assert store.save_month("2026-02", "Globex", "GX Dev", "2,100", "1700") is True
        assert self.text(settings) == WORK_CSV + "\n2026-02;Globex;GX Dev;2100.00;1700.00"
        assert store.save_month("2026-03") is True  # a gap
        assert self.text(settings).endswith("\n2026-03;;;;")

    def test_add_before_the_first_month(self, store, settings):
        store.save_month("2025-09", "Acme", "Acme Jr", "900", "700")
        assert self.text(settings).startswith(
            "Month;Company;Position;Gross salary;Net salary\n2025-09;Acme;Acme Jr;900.00;700.00\n2025-10;"
        )

    def test_existing_months_are_replaced(self, store, settings):
        assert store.save_month("2025-12", "Acme", "Acme Sr", "1500.5", "1200") is False
        assert "\n2025-12;Acme;Acme Sr;1500.50;1200.00\n" in self.text(settings)
        assert store.save_month("2025-11") is False  # now a gap
        assert "\n2025-11;;;;\n" in self.text(settings)
        assert len(store.load_work().months) == 4

    def test_payment_date_adds_its_column(self, store, settings):
        store.save_month("2025-11", "Acme", "Acme Sr", "1500.5", "1200", "2025-12-05")
        assert self.text(settings) == (
            "Month;Company;Position;Gross salary;Net salary;Received payment date\n"
            "2025-10;Acme;Acme Jr;1000.00;800.00;\n"
            "2025-11;Acme;Acme Sr;1500.50;1200.00;2025-12-05\n"
            "2025-12;;;;;\n"
            "2026-01;Globex;GX Dev;2000.00;1600.00;"
        )
        assert store.load_work().months[1].payment_date == dt.date(2025, 12, 5)
        store.save_month("2026-02", "Globex", "GX Dev", "1", "1")
        assert self.text(settings).endswith("\n2026-02;Globex;GX Dev;1.00;1.00;")

    @pytest.mark.parametrize(
        ("args", "error"),
        [
            (("2026-03",), "Months cannot be skipped: add 2026-02 \\(after the last month\\) first"),
            (("2025-08",), "Months cannot be skipped"),
            (("2026-02", "Globex", "", "1", "1"), "both a company and a job position"),
            (("2026-02", "", "", "1", "1"), "cannot have a salary"),
            (("2026-02", "Globex", "GX Dev", "1", ""), "both a gross and a net salary"),
            (("2026-02", "Globex", "GX Dev", "1", "2"), "over the gross"),
            (("2026-02", "Globex", "GX Dev", "1", "-1"), "cannot be negative"),
            (("2026-02", "Globex", "GX Dev", "1", "1", "2026-02-30"), "Invalid received payment date"),
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
        assert card_labels(r.text) == ["Current job", "Gross", "Net"]
        assert "<strong>Globex</strong> <span class=\"badge gain\">Current</span>" in r.text
        salaries = r.text.split('data-sortable="salaries"')[1]
        assert re.findall(r"<strong>(\d{4}-\d{2})</strong>", salaries) == ["2026-01", "2025-12", "2025-11", "2025-10"]
        assert '<td class="muted" colspan="2">No job</td>' in salaries
        assert "$1,500.50" in salaries
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
        store.save_month("2025-11", "Acme", "Acme Sr", "1500.5", "1200", "2025-12-05")
        store.save_month("2026-01", "Globex", "GX Dev", "2000", "1600", "2026-02-05")
        r = client.get("/work")
        assert "⚠ 1 past month with a job has no received payment date: 2025-10." in r.text
        store.save_month("2025-10", "Acme", "Acme Jr", "1000", "800", "2025-11-05")
        assert "no received payment date" not in client.get("/work").text

    def test_current_month_is_not_unpaid(self, client, store, settings):
        today = dt.date.today()
        settings.work_path.write_text(
            "Month;Company;Position;Gross salary;Net salary;Received payment date\n"
            f"{today:%Y-%m};Acme;Dev;10;8;\n",
            encoding="utf-8",
        )
        r = client.get("/work")
        assert "no received payment date" not in r.text and "⚠ Not received" not in r.text

    def test_edit_form(self, client):
        r = client.get("/work/form", params={"month": "2025-11"})
        assert "<h2>Edit 2025-11</h2>" in r.text and 'value="1,500.50"' in r.text
        assert "Save 2025-11" in r.text
        r = client.get("/work/form", params={"month": "2026-02"})
        assert "<h2>New month</h2>" in r.text

    def test_forms(self, client, store):
        r = client.post("/work/months", data={"month": "2026-03"}, headers=HTMX)
        assert "Months cannot be skipped" in r.text and 'value="2026-03"' in r.text
        r = client.post(
            "/work/months",
            data={"month": "2026-02", "company": "Globex", "position": "GX Dev", "gross": "2,000.00", "net": "1,600.00"},
            headers=HTMX,
        )
        assert "Added 2026-02 (GX Dev at Globex)" in r.text
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
        r = client.put("/api/work/months", json={"month": "2026-02", "company": "Globex", "position": "GX Dev", "gross": 10, "net": 8})
        assert r.status_code == 200 and r.json()["created"] is True
        assert client.get("/api/work/months").json()[-1] == {
            "month": "2026-02", "company": "Globex", "position": "GX Dev",
            "gross": 10.0, "net": 8.0, "payment_date": None,
        }
        r = client.put("/api/work/months", json={"month": "2026-04"})
        assert r.status_code == 422 and "cannot be skipped" in r.json()["detail"]


def test_work_file_is_created_when_adding(settings, tmp_path):
    settings = replace(settings, work_path=tmp_path / "w" / "work.csv")
    r = TestClient(create_app(settings)).get("/work")
    assert r.status_code == 200 and "No companies yet" in r.text and "No months yet." in r.text
    store = LedgerStore(settings)
    store.save_month("2025-01", "Acme", "Dev", "10", "8", "2025-02-01")
    assert settings.work_path.read_text(encoding="utf-8") == (
        "Month;Company;Position;Gross salary;Net salary;Received payment date\n"
        "2025-01;Acme;Dev;10.00;8.00;2025-02-01\n"
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
    # Each salary in its own color
    assert re.findall(r'style="--line: var\((--series-\d)\)"', r.text) == ["--series-1", "--series-2"]


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
        return replace(settings, creditors_path=creditors, work_path=work)

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
        store.save_month("2026-02", "Initech", "Dev", "10", "8")
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
        for title, count in (("People", 1), ("Services", 1), ("Companies", 3)):
            assert f"<h2>{title} <small>{count}</small></h2>" in r.text
        companies = r.text.split("<h2>Companies")[1].split("</table>")[0]
        assert "<strong>Globex</strong></td>\n        <td>Settled</td>" in companies
        assert "<strong>Dad</strong>" not in companies
        we_owe = r.text.split("We owe <span")[1].split("Owed to us <span")[0]
        assert '<span class="bar-name">Services</span>' in we_owe and '<span class="bar-name">Gas</span>' in we_owe
        owed = r.text.split("Owed to us <span")[1].split("<h2>People")[0]
        assert '<span class="bar-name">Companies</span>' in owed and "People</span>" not in owed
