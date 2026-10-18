"""Tests for pledger.web."""

import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from pledger.models import INVESTMENT
from pledger.web.app import create_app
from pledger.web.store import LedgerStore, Settings, _insert_yaml_entry, row_fingerprint
from pledger.web.templating import money, quantity
from pledger.web.views import build_charts, composition, equity_series, nice_ticks

HTMX = {"HX-Request": "true"}


@pytest.fixture
def settings(accounts_yaml, base_movements_yaml, ledger_csv, currencies_yaml) -> Settings:
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
        with settings.base_movements_path.open("a") as f:
            f.write("internal:\n  transfer:\n    Adjust: Set by pledger\n")
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
        assert (result_types[3].name, result_types[3].long_name) == ("Tax", "Taxes")
        data = yaml.safe_load(settings.base_movements_path.read_text())
        assert data["result_types"]["Tax"] == {
            "id": 3, "long_name": "Taxes", "description": "Taxes paid",
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
        assert "$960.25" in r.text  # profit card
        assert "Balances" not in r.text and "New account" not in r.text
        assert card_labels(r.text) == ["Gains", "Losses", "Profit"]

    def test_accounts(self, client):
        r = client.get("/accounts")
        assert r.status_code == 200
        assert 'href="/accounts" aria-current="page"' in r.text
        assert card_labels(r.text) == ["Assets", "Liabilities", "Equity"]
        cards = r.text.split('<div class="cards">')[1].split("</div>\n    </div>")[0]
        assert cards.count("$960.25") == 2  # assets (800.50 + 159.75) and equity
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

    def test_accounts_grouped_by_currency_or_liquidity(self, client):
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
        assert "$800.50" in r.text and "$159.75" in r.text  # Bk and Ca balances
        assert "Salary" in r.text and "Food" in r.text
        assert "by result type" in r.text and "Result by type" in r.text
        assert "$960.25" in r.text  # final equity
        assert 'data-sortable="equity"' in r.text

    def test_charts_partial_for_month(self, client, settings):
        with settings.ledger_path.open("a") as f:
            f.write("2026-02-01;Food;Eq;100;Ca;\n")
        r = client.get("/charts", params={"month": "2026-02"}, headers=HTMX)
        assert "<html" not in r.text
        assert "as of end of 2026-02" in r.text
        assert "Salary" in r.text  # no gains in February, but still listed at zero
        assert "-$100.00" in r.text and "$860.25" in r.text


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
        assert [r["name"] for r in charts["assets"]] == ["Ca", "Bk"]  # accounts.yaml order
        assert [r["name"] for r in charts["liabilities"]] == ["Cc"]
        losses = charts["losses"][0]["children"]
        assert [r["name"] for r in losses] == ["Food", "Card fee"]  # definition order, not size
        gains = charts["gains"]
        assert [r["name"] for r in gains] == ["Operative", "Gifts"]  # result type id order

    def test_charts_list_everything_even_at_zero(self, settings):
        with settings.ledger_path.open("a") as f:
            f.write("2026-02-01;Food;Eq;100;Ca;\n")
        charts = build_charts(LedgerStore(settings).load(), "2026-02")
        # Only Food moved in February; every other bar is still there, at zero
        assert [r["name"] for r in charts["assets"]] == ["Ca", "Bk"]
        assert [r["name"] for r in charts["liabilities"]] == ["Cc"]
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
        assert [r["name"] for r in charts["losses"]] == ["Operative", "Gifts"]  # Gifts at zero

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
        assert [(p["date"], p["value"]) for p in series] == [
            ("2026-01-01", 1000.5), ("2026-01-03", 950.25), ("2026-01-04", 960.25),
        ]
        chart = build_charts(snapshot)["equity"]
        assert chart["points"][0]["x"] == 0 and chart["points"][-1]["x"] == 100
        assert chart["change"] == pytest.approx(-40.25)  # since the first entry
        assert build_charts(snapshot, "2025-12")["equity"] is None


class TestApi:
    def test_summary(self, client):
        data = client.get("/api/summary").json()
        assert data["profit"] == pytest.approx(960.25)
        assert data["balances"]["Bk"] == pytest.approx(800.50)
        assert data["assets"] == pytest.approx(960.25)
        assert data["liabilities"] == pytest.approx(0)
        assert data["equity"] == pytest.approx(960.25)

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
        assert [rt["name"] for rt in client.get("/api/result-types").json()] == ["Op", "Gifts"]
        body = {"name": "Tax", "long_name": "Taxes", "description": "d"}
        assert client.post("/api/result-types", json=body).status_code == 201
        assert client.post("/api/result-types", json=body).status_code == 422
        result_types = client.get("/api/result-types").json()
        assert result_types[-1] == {
            "id": 3, "name": "Tax", "long_name": "Taxes", "description": "d", "internal": False,
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
    @pytest.fixture
    def settings(self, settings):
        with settings.base_movements_path.open("a") as f:
            f.write(
                "internal:\n  Init:\n    id: 3\n    long_name: Initial\n    description: d\n"
                "    base_movements:\n      gain:\n        Initial (G): Initial balance\n"
            )
        return settings

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

    def test_investment_is_not_an_account_name(self, settings):
        with pytest.raises(ValueError, match="'INVESTMENT' is reserved"):
            LedgerStore(settings).add_account("INVESTMENT", "asset", "d", "0")


class TestDebtAccounts:
    def test_debt_accounts_are_loaded(self, settings, tmp_path):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace("    description: Credit card\n    initial_amount: 0\n", "    description: Credit card\n"),
            encoding="utf-8",
        )
        path = tmp_path / "creditors.yaml"
        path.write_text("Cc:\n  Dad: 10\n", encoding="utf-8")
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
        assert '<span class="num ">$500.00</span>' in r.text
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
        path.write_text("Cc:\n  Dad: 100\n  Bank: 50.5\n  Ana: -30\n  Leo: 0\n", encoding="utf-8")
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
        assert 'data-sortable="debts"' in r.text
        for name, role in (("Dad", "Creditor"), ("Ana", "Debtor"), ("Leo", "Settled")):
            assert f"<strong>{name}</strong></td>\n        <td>{role}</td>" in r.text

    def test_debt_bars_show_positive_amounts(self, client):
        r = client.get("/debt")
        owed = r.text.split("Owed to us <span")[1].split("<h2>People</h2>")[0]
        assert '<span class="bar-name">Ana</span>' in owed and "-$30.00</span>" not in owed
        we_owe = r.text.split("We owe <span")[1].split("Owed to us <span")[0]
        assert '<span class="bar-name">Dad</span>' in we_owe
        assert "Leo" not in we_owe and "Ana" not in we_owe


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
