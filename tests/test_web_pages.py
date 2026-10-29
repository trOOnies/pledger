"""Tests for the pages and the API of pledger.web, shared by every tab."""

import datetime as dt
import re
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import HTMX, card_labels, rows

from pledger.web.app import create_app
from pledger.web.routers import server
from pledger.web.store import (
    LedgerStore,
    StaleRowError,
    row_fingerprint,
)
from pledger.web.views import (
    build_accounts,
    build_currencies,
    build_results,
    composition,
    equity_series,
    nice_ticks,
)


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
        form = {"name": "Fx", "acc_type": "asset", "description": "Fictitious", "initial_amount": "0", "check": "net_zero"}
        r = client.post("/accounts", data=form, headers=HTMX)
        assert "Added account Fx" in r.text and r.headers["HX-Trigger"] == "accounts-changed"
        r = client.get("/accounts", headers=HTMX)  # the rest of the tab reloads
        fi_row = r.text.split("<strong>Fx</strong>")[1].split("</tr>")[0]
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
        assert "Added account Sv" in r.text and r.text.lstrip().startswith('<form id="account-form"')  # the form alone
        assert "<strong>Sv</strong>" in client.get("/accounts", headers=HTMX).text
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
        assert [c["name"] for r in by_currency["assets"] for c in r["children"]] == ["Ca", "Fi", "Bk"]
        assert [c["name"] for r in by_currency["liabilities"] for c in r["children"]] == ["Cc", "De"]
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
        assert list(accounts) == ["Ca", "Bk", "Cc", "Fi", "De", "Eq"]
        assert accounts["Bk"] == {
            "name": "Bk", "description": "Bank account", "type": "asset", "currency": "USD",
            "is_liquid": True, "initial_amount": 0.1005, "initial_date": "2025-12-31", "checks": [],
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


@pytest.mark.parametrize(
    ("path", "message"),
    [("/investments", "No investment accounts yet")],
)
def test_pages_without_special_accounts(client, path, message):
    r = client.get(path)
    assert r.status_code == 200 and message in r.text


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
        stopped = []
        monkeypatch.setattr(server, "stop_server", lambda: stopped.append(True))
        assert "L-1" not in settings.ledger_path.read_text()
        r = client.post("/quit")
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert stopped == [True]
        assert "L-1" in settings.ledger_path.read_text()

    def test_button_in_every_page(self, client):
        assert 'id="quit"' in client.get("/").text and 'id="quit"' in client.get("/help").text
        # An open door icon, like the eye of the visibility toggle
        button = client.get("/").text.split('id="quit"')[1].split("</button>")[0]
        assert 'aria-label="Save &amp; Quit"' in button and "<svg" in button and "Save &amp; Quit</button>" not in button


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
