"""Tests for the Investments tab: investments, their movements and prices."""

import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import (
    HTMX,
    INVEST_TRANSFERS_YAML,
    INVEST_YAML,
    INVESTMENT_HEADER,
    card_labels,
    investment_rows,
    movement_ids,
    rows,
    with_internal,
    with_prices,
)

from pledger.enums import InvestmentType
from pledger.models import CREDIT_CARD, INVESTMENT
from pledger.web.app import create_app
from pledger.web.store import (
    LedgerDataError,
    LedgerStore,
    interest_fingerprint,
    row_fingerprint,
)
from pledger.web.views import (
    build_invest_movements,
    build_investments,
    investment_charts,
)


class TestInvestments:
    def test_investment_accounts_are_loaded(self, settings, tmp_path):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace("liability:\n", "  Br:\n    description: Broker\nliability:\n"), encoding="utf-8"
        )
        path = tmp_path / "investment.yaml"
        path.write_text("Br:\n  T:\n    type: stock\n    quantity: 1\n", encoding="utf-8")
        settings = with_prices(settings, "previous_month;Br;T;10\n")
        accounts = LedgerStore(replace(settings, investments_path=path)).load().accounts
        assert accounts["Br"].investments["T"]["total_quantity"] == 1.0
        assert accounts["Br"].initial_amount == 10.0


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

    @pytest.mark.parametrize("name", [INVESTMENT, CREDIT_CARD, "FOREIGN CURRENCY"])
    def test_is_not_an_account_name(self, settings, name):
        with pytest.raises(ValueError, match=f"'{name}' is reserved"):
            LedgerStore(settings).add_account(name, "asset", "d", "0")


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
            "Br:\n  T:\n    type: stock\n    quantity: 1.5\n"
            "  AL30:\n    type: public_bond\n    quantity: 1000\n"
            "Br2:\n  T:\n    type: stock\n    quantity: 0.25\n"
            "  QQQ: etf\n",
            encoding="utf-8",
        )
        settings = with_prices(settings, "previous_month;Br;T;200\nprevious_month;Br;AL30;0.1\nprevious_month;Br2;T;400\n")
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
        assert '<th class="num" title="Its last price: amount over quantity">Price</th>' in r.text
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


class TestNewInvestment:
    @pytest.fixture
    def settings(self, settings, tmp_path):
        text = settings.accounts_path.read_text(encoding="utf-8")
        settings.accounts_path.write_text(
            text.replace("liability:\n", "  Br:\n    description: Broker\nliability:\n"), encoding="utf-8"
        )
        path = tmp_path / "investment.yaml"
        path.write_text("Br:\n  T: stock\n", encoding="utf-8")
        # No prices yet: created when adding the first one
        return replace(settings, investments_path=path, prices_path=tmp_path / "prices.csv")

    def test_add_investment(self, settings):
        store = LedgerStore(settings)
        store.add_investment("Br", " SPY ", "etf")
        assert not settings.prices_path.exists()
        # Its initial quantity at its initial price, in the prices CSV
        store.add_investment("Br", "AL30", "public_bond", "10", "1,500")
        assert settings.investments_path.read_text() == (
            "Br:\n  T: stock\n  SPY: etf\n  AL30:\n    type: public_bond\n    quantity: 10.0\n"
        )
        assert settings.prices_path.read_text() == "date;account;investment;price\nprevious_month;Br;AL30;1,500.00\n"
        held = store.load().accounts["Br"]
        assert held.investments["AL30"]["total_amount"] == 15000 and held.initial_amount == 15000
        for args, error in [
            (("Br", "T", "etf"), "Investment 'T' already exists"),
            (("Ca", "X", "etf"), "'Ca' is not an investment account"),
            (("Br", "X", "gold"), "Unknown investment type: 'gold'"),
            (("Br", " ", "etf"), "name is required"),
            (("Br", "X", "etf", "-1"), "cannot be negative"),
            (("Br", "X", "etf", "1"), "An initial quantity needs its initial price"),
            (("Br", "X", "etf", "1", "-5"), "cannot be negative"),
        ]:
            with pytest.raises(ValueError, match=re.escape(error)):
                store.add_investment(*args)
        assert "X" not in settings.investments_path.read_text() and "X" not in settings.prices_path.read_text()

    def test_add_investment_rolls_back_both_files(self, settings):
        # The initial price can't be appended to a prices CSV with multi-line fields
        prices = 'date;account;investment;price\nprevious_month;Br;T;"1\n"\n'
        settings.prices_path.write_text(prices)
        with pytest.raises(ValueError, match="multi-line fields"):
            LedgerStore(settings).add_investment("Br", "X", "etf", "1", "2")
        assert settings.investments_path.read_text() == "Br:\n  T: stock\n"
        assert settings.prices_path.read_text() == prices

    def test_new_investment_form(self, settings):
        client = TestClient(create_app(settings))
        r = client.get("/investments")
        assert "<h2>New investment</h2>" in r.text  # even without investment base movements
        assert "<h2>New investment movement</h2>" not in r.text
        form = r.text.split('id="new-investment-form"')[1].split("</form>")[0]
        assert "<option selected>Br</option>" in form or "<option >Br</option>" in form
        assert '<option value="etf"' in form and 'name="price"' in form and 'name="quantity"' in form
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
        with_internal(path, INVEST_YAML, INVEST_TRANSFERS_YAML)
        # Dividends of T
        movements = tmp_path / "investment.csv"
        movements.write_text(INVESTMENT_HEADER + "2026-01-05;Dividends;Ca;5;Eq;;T;\n")
        investments = tmp_path / "investment.yaml"
        investments.write_text("Br:\n  T:\n    type: stock\n    quantity: 10\n", encoding="utf-8")
        # T at 10 initially, then at 10.5: a capital gain of 5
        settings = with_prices(settings, "previous_month;Br;T;10\n2026-01-05;Br;T;10.5\n")
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

    @staticmethod
    def add_u(settings):
        """Another investment of Br: 1 of U, at 50."""
        settings.investments_path.write_text(settings.investments_path.read_text() + "  U:\n    type: etf\n    quantity: 1\n")
        with settings.prices_path.open("a") as f:
            f.write("previous_month;Br;U;50\n")

    def test_edit_investment_movement(self, settings, client):
        LedgerStore(settings).freeze_ids()
        fp = row_fingerprint(LedgerStore(settings).load().investments.iloc[0])
        assert 'hx-get="/investments/movements/0/edit"' in client.get("/investments").text
        r = client.get("/investments/movements/0/edit", params={"fingerprint": fp})
        assert "Edit investment movement" in r.text and "<small>I-1</small>" in r.text
        assert 'hx-post="/investments/movements/0/edit"' in r.text and 'value="5.00"' in r.text
        r = client.post("/investments/movements/0/edit", data={
            "fingerprint": fp, "move_date": "2026-01-05", "move_type": "gain", "base_movement": "Dividends",
            "acc_debit": "Ca", "acc_credit": "Eq", "amount": "7", "investment": "T",
        })
        assert "Updated I-1 Dividends of T: $7.00" in r.text and r.headers["HX-Trigger"] == "investments-changed"
        assert settings.investment_movements_path.read_text().splitlines()[-1] == "2026-01-05;Dividends;Ca;7.00;Eq;;T;;I-1"
        assert client.get("/investments/movements/0/edit", params={"fingerprint": fp}).status_code == 409
        r = client.post("/investments/movements/0/edit", data={
            "fingerprint": row_fingerprint(LedgerStore(settings).load().investments.iloc[0]), "move_date": "2026-01-05",
            "base_movement": "Investment - Bought", "acc_debit": "Br", "acc_credit": "Ca", "amount": "7",
            "investment": "Nope", "quantity": "1",
        })
        assert 'class="msg error"' in r.text and "Edit investment movement" in r.text
        # The capital gains are generated from the prices: they can't be edited
        assert "Capital gains" not in client.get("/form/invest-bases", params={"move_type": "gain"}).text
        r = client.post("/investments/movements/0/edit", data={
            "fingerprint": row_fingerprint(LedgerStore(settings).load().investments.iloc[0]), "move_date": "2026-01-05",
            "base_movement": "Capital gains", "acc_debit": "Br", "acc_credit": "Eq", "amount": "7", "investment": "T",
        })
        assert "are generated by pledger" in r.text

    def test_investment_filter(self, settings, client):
        self.add_u(settings)
        self.append(settings, "2026-01-06;Dividends;Ca;3;Eq;;U;\n")
        r = client.get("/investments")
        assert '<form id="invest-filter"' in r.text and 'name="account"' in r.text
        assert "<option >Br</option>" in r.text and "<option >U</option>" in r.text
        r = client.get("/investments/movements", params={"investment": "U"})
        assert "<h2>Investment movements <small>1</small></h2>" in r.text and "$3.00" in r.text
        assert "<option selected>U</option>" in r.text and 'hx-swap-oob="true"' in r.text  # the filter, out of band
        for params, n in (({"account": "Br"}, 3), ({"account": "Nope"}, 3), ({"investment": "Nope"}, 3), ({}, 3)):
            assert f"<h2>Investment movements <small>{n}</small></h2>" in client.get("/investments/movements", params=params).text
        # The movements reload with the investment picked
        assert 'hx-include="#invest-filter"' in client.get("/investments").text

    def test_investment_filter_keeps_the_movements_off_the_investment_account(self, settings, client):
        # Broker fees of T, out of Ca: still a movement of T, and of Br, the account holding it
        self.append(settings, "2026-01-06;Broker fees;Eq;1;Ca;;T;\n")
        moves = [m for m in build_invest_movements(LedgerStore(settings).load()) if m["investment"] == "T"]
        assert {m["account"] for m in moves} == {"Br"} and "Broker fees" in {m["base"] for m in moves}
        for params in ({"investment": "T"}, {"account": "Br"}, {"account": "Br", "investment": "T"}):
            r = client.get("/investments/movements", params=params)
            assert f"<h2>Investment movements <small>{len(moves)}</small></h2>" in r.text and "Broker fees" in r.text
        # An investment of another account is dropped
        r = client.get("/investments/movements", params={"account": "Br", "investment": "Nope"})
        assert f"<h2>Investment movements <small>{len(moves)}</small></h2>" in r.text and "<option selected>Br</option>" in r.text and "<option >T</option>" in r.text

    def test_running_totals(self, settings, client):
        self.append(settings, "2026-01-06;Investment - Bought;Br;11;Ca;1;T;\n2026-01-07;Broker fees;Eq;1;Ca;;T;\n")
        moves = build_invest_movements(LedgerStore(settings).load())
        # T: 10 at 10 initially, at 10.5 at the end of 2026-01-05, at 11 right before buying 1 more at 11
        assert [(m["base"], m["account_total"], m["investment_amount"], m["investment_quantity"]) for m in moves] == [
            ("Broker fees", pytest.approx(121), pytest.approx(121), pytest.approx(11)),
            ("Investment - Bought", pytest.approx(121), pytest.approx(121), pytest.approx(11)),
            ("Capital gains", pytest.approx(110), pytest.approx(110), pytest.approx(10)),
            ("Capital gains", pytest.approx(105), pytest.approx(105), pytest.approx(10)),
            ("Dividends", pytest.approx(100), pytest.approx(100), pytest.approx(10)),
        ]
        assert [m["id"] for m in moves if m["source"] == "prices"] == ["P-Br-T-2026-01-06-1", "P-Br-T-2026-01-05"]
        assert [m["comment"] for m in moves if m["source"] == "prices"] == ["10.00 T at 11.00", "10.00 T at 10.50"]
        table = client.get("/investments/movements").text
        assert "Account total</th>" in table and "Investment quantity</th>" in table and ">11</td>" in table

    def test_base_movement_filter(self, settings, client):
        self.append(settings, "2026-01-07;Broker fees;Eq;1;Ca;;T;\n")
        r = client.get("/investments/movements", params={"base": "Broker fees"})
        assert "<h2>Investment movements <small>1</small></h2>" in r.text and "<option selected>Broker fees</option>" in r.text
        r = client.get("/investments/movements", params={"base": "Nope"})
        assert "<h2>Investment movements <small>3</small></h2>" in r.text

    def test_form_narrows_the_investments_by_account(self, client):
        f = client.get("/form/invest-fields", params={"base_movement": "Capital gains"}).text
        assert 'name="investment_account" data-narrows="investment"' in f and "<option >Br</option>" in f
        assert '<optgroup label="Br">' in f

    def test_broker_fees_over_amount_and_totals_row(self, settings, client):
        self.add_u(settings)
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
        body = {**data, "base_movement": "Dividends", "acc_debit": "Ca", "acc_credit": "Eq", "amount": 2,
                "investment": "T"}
        assert client.post("/api/investments/movements", json=body).status_code == 201
        assert client.post("/api/movements", json=body).status_code == 422
        # The capital gains and losses are generated from the prices
        r = client.post("/api/investments/movements", json={**body, "base_movement": "Capital gains", "acc_debit": "Br"})
        assert r.status_code == 422 and "are generated by pledger" in r.json()["detail"]
        assert list(investment_rows(settings).base_movement) == ["Dividends", "Broker fees", "Dividends"]
        assert len(rows(settings)) == 4  # the ledger CSV is left as is
        # The INVESTMENT side still takes only investment accounts
        r = client.post("/investments/movements", data={**data, "base_movement": "Investment - Bought",
                                                         "acc_debit": "Ca", "acc_credit": "Bk", "investment": "T",
                                                         "quantity": "1"})
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
        assert "<option selected>Dividends</option>" in bases and "Broker fees" not in bases
        assert "Capital gains" not in bases  # generated from the prices
        assert 'id="invest-fields"' in bases
        assert "<h2>Investment movements <small>2</small></h2>" in r.text
        # The capital gain of the price of 2026-01-05, at the end of that date
        moves = client.get("/api/investments/movements").json()
        assert [(m["base"], m["source"], m["id"]) for m in moves] == [
            ("Capital gains", "prices", "P-Br-T-2026-01-05"), ("Dividends", "investments", None),  # until frozen
        ]
        LedgerStore(client.app.state.store.settings).freeze_ids()
        assert movement_ids(client.get("/investments").text) == ["P-Br-T-2026-01-05", "I-1"]
        assert {"I-1", "P-Br-T-2026-01-05"} <= set(movement_ids(client.get("/").text))
        assert 'hx-post="/investments/movements/0/delete"' in r.text
        assert r.text.count('aria-label="Delete movement"') == 1  # never the generated ones
        # The ledger shows them with a lock that leads to the Investments tab
        index = client.get("/").text
        assert 'href="/investments#movement-I-1" title="Added, edited and deleted from the Investments tab" aria-label="Added, edited and deleted from the Investments tab">🟢</a>' in index
        assert 'href="/investments#movement-P-Br-T-2026-01-05" title="Generated from the prices' in index
        assert '<tr id="movement-I-1">' in client.get("/investments").text
        assert index.count('aria-label="Delete movement"') == 4

    def test_interest_on_money(self, settings, client):
        assert card_labels(client.get("/investments").text) == ["Invested", "Net capital gains", "Dividends", "Total result"]
        path = settings.base_movements_path
        path.write_text(
            path.read_text()
            .replace("Dividends: d\n", "Dividends: d\n          Interest on Money (G): d\n", 1)
            .replace("Broker fees: d\n", "Broker fees: d\n          Interest on Money (L): d\n", 1)
        )
        r = client.get("/investments")
        assert card_labels(r.text) == ["Invested", "Net interest on money", "Net capital gains", "Dividends", "Total result"]
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
        # Capital gains of 5, the 5 and 7 of dividends of T and the net interest of 4
        assert context["result_totals"]["interest"] == 4
        assert context["result_totals"]["total"] == 21
        assert [p["value"] for p in context["result_series"]] == [0, 13, 21]
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
            path.write_text(path.read_text().replace("Dividends: d\n", "Dividends: d\n          Interest on Money (G): d\n", 1))
        settings.interest_path.write_text("move_date;acc_debit;amount;comment\n" + line + "\n")
        with pytest.raises(LedgerDataError, match=re.escape(message)):
            LedgerStore(settings).load()

    def test_invested_and_result_over_time(self, settings, client):
        snapshot = LedgerStore(settings).load()
        charts = investment_charts(snapshot)
        # T starts at 100 on the first ledger date, then gains 5 in 2026-01, with 5 of dividends
        assert [(p["month"], p["value"], p["change"]) for p in charts["invested_series"]] == [
            ("2025-12", 100.0, 100.0), ("2026-01", 105.0, 5.0)
        ]
        assert [(p["month"], p["value"]) for p in charts["result_series"]] == [("2025-12", 0.0), ("2026-01", 10.0)]
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
        assert list(investment_rows(settings).base_movement) == ["Dividends", "Investment - Bought"]

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
        # Bought at 400 (12.5 held), sold at 75: the 8.5 left at 75 too
        assert self.holding(settings) == (InvestmentType.STOCK, 637.5, 8.5)
        assert self.holding(settings, "NEW") == (None, 50, 1)

    @pytest.mark.parametrize(
        ("row", "message"),
        [
            ("2026-01-06;Investment - Sold;Ca;1;Br;11;T;", r"quantity of 'T' .* cannot be negative"),
            ("2026-01-06;Investment - Bought;Br;1;Ca;;T;", "must have a quantity"),
            ("2026-01-06;Investment - Bought;Br;1;Ca;1;;", "must name their investment"),
            ("2026-01-06;Dividends;Ca;1;Eq;1;T;", "cannot have a quantity"),
            ("2026-01-06;Capital gains;Br;1;Eq;;T;", "cannot have movements of 'Capital gains' .* generated from the prices"),
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
                              "2026-01-10;Investment - Sold;Ca;103;Br;10;T;\n"  # all of it
                              "2026-01-10;Broker fees;Eq;4;Ca;;;\n"  # names no investment
                              "2026-01-11;Dividends;Ca;7;Eq;;GHOST;\n")
        with settings.prices_path.open("a") as f:
            f.write("2026-01-09;Br;T;10.3\n")  # a capital loss of 2
        unattributed = []
        br = LedgerStore(settings).load().investment_accounts(unattributed)[0]
        assert br.results == {"T": {"Dividends": 17.5, "Capital gains": 5.0, "Broker fees": 1.0, "Capital losses": 2.0}}
        assert br.investments["T"]["total_amount"] == pytest.approx(0)  # 100 + 5 - 2 - 103
        assert [(u["investment"], u["amount"]) for u in unattributed] == [("GHOST", 7.0)]
        r = client.get("/investments")
        assert card_labels(r.text)[-4:] == ["Net capital gains", "Dividends", "Broker fees", "Total result"]
        closed = r.text.split("<h2>No longer held")[1].split("</table>")[0]
        assert ">Net capital gains</th>" in closed and ">Total result</th>" in closed
        # Sold, but its capital gains (5 - 2) and total result (3 + 17.50 - 1) stay
        for value in ("3.0\">$3.00", "17.5\">$17.50", "1.0\">$1.00", "19.5\">$19.50"):
            assert f'data-sort="{value}</td>' in closed
        assert "⚠ 1 movement names an investment of no investment account:\n      GHOST." in r.text

    def test_prices_revalue_at_the_end_of_their_date(self, settings):
        # A price on the date of a purchase comes after it: what was bought is revalued too
        self.append(settings, "2026-01-08;Investment - Bought;Br;24;Ca;2;T;\n")
        with settings.prices_path.open("a") as f:
            f.write("2026-01-08;Br;T;12.5\n2026-01-20;Br;T;12.5\n")
        snapshot = LedgerStore(settings).load()
        assert [(r.id, r.base_movement, r.amount, r.comment) for r in snapshot.capital_rows.itertuples()] == [
            ("P-Br-T-2026-01-05", "Capital gains", "5.00", "10.00 T at 10.50"),
            ("P-Br-T-2026-01-08-1", "Capital gains", "15.00", "10.00 T at 12.00"),
            ("P-Br-T-2026-01-08", "Capital gains", "6.00", "12.00 T at 12.50"),
            # none at the same price
        ]
        assert self.holding(settings) == (InvestmentType.STOCK, 150, 12)
        # In the ledger, against equity
        assert snapshot.ledger().balances()["Br"] == pytest.approx(150)

    def test_prices_without_quantity_change_nothing(self, settings):
        self.add_u(settings)
        self.append(settings, "2026-01-06;Investment - Sold;Ca;60;Br;1;U;\n")  # a gain of 10 right before
        with settings.prices_path.open("a") as f:
            f.write("2026-01-07;Br;U;70\n")
        assert list(LedgerStore(settings).load().capital_rows.id) == ["P-Br-T-2026-01-05", "P-Br-U-2026-01-06-1"]
        assert self.holding(settings, "U") == (InvestmentType.ETF, 0, 0)

    def test_capital_losses_are_required_for_a_loss(self, settings):
        path = settings.base_movements_path
        path.write_text(path.read_text().replace("          Capital losses:\n            description: d\n            credit: INVESTMENT\n", ""))
        LedgerStore(settings).load()  # no loss yet
        with settings.prices_path.open("a") as f:
            f.write("2026-01-06;Br;T;9\n")
        with pytest.raises(LedgerDataError, match="'Capital losses' is required to update the prices of the investments"):
            LedgerStore(settings).load()

    @pytest.mark.parametrize(
        ("row", "message"),
        [
            ("2026-01-06;Ca;T;1", "prices of 'Ca', which is not an investment account"),
            ("2026-01-06;Br;NOPE;1", "prices of 'NOPE', which is not an investment of Br"),
            ("2026-13-06;Br;T;1", "Invalid date of the prices CSV"),
            ("2026-01-05;Br;T;11", "more than one price of T of Br on 2026-01-05"),
            ("previous_month;Br;T;11", "more than one price of T of Br on previous_month"),
            ("2026-01-06;Br;T;-1", "price of T of Br on 2026-01-06 cannot be negative"),
            ("2026-01-06;Br;T;x", "Invalid price of T of Br on 2026-01-06"),
            ("2026-01-06;;T;1", "must have their account and investment"),
        ],
    )
    def test_invalid_prices(self, settings, row, message):
        with settings.prices_path.open("a") as f:
            f.write(row + "\n")
        with pytest.raises(LedgerDataError, match=message):
            LedgerStore(settings).load()

    def test_add_price(self, settings, client):
        store = LedgerStore(settings)
        settings.prices_path.write_text(settings.prices_path.read_text().rstrip("\n"))  # no line break at the end
        store.add_price("2026-01-06", "Br", " T ", "11")
        assert settings.prices_path.read_text().endswith("2026-01-05;Br;T;10.5\n2026-01-06;Br;T;11.00")
        assert self.holding(settings) == (InvestmentType.STOCK, 110, 10)
        for args, error in [
            (("2026-01-06", "Br", "T", "12"), "T of Br already has a price on 2026-01-06"),
            (("2026-01-06", "Ca", "T", "12"), "'Ca' is not an investment account"),
            (("2026-01-06", "Br", "NOPE", "12"), "'NOPE' is not an investment of Br"),
            (("2026-01-06", "Br", "T", "-1"), "The price cannot be negative"),
            (("", "Br", "T", "12"), "The date is required"),
        ]:
            with pytest.raises(ValueError, match=re.escape(error)):
                store.add_price(*args)
        assert settings.prices_path.read_text().count("\n") == 3

        # From the Investments tab, and the API
        r = client.get("/investments")
        form = r.text.split('id="price-form"')[1].split("</form>")[0]
        assert "<h2>New price</h2>" in r.text and 'name="price"' in form and '<optgroup label="Br">' in form
        assert 'hx-get="/form/price" hx-trigger="investment-added from:body"' in r.text
        data = {"date": "2026-01-07", "account": "Br", "investment": "T", "price": "11.5"}
        r = client.post("/investments/prices", data=data)
        assert "Added the price of T on 2026-01-07: $11.50" in r.text and r.headers["HX-Trigger"] == "investments-changed"
        assert 'value="2026-01-07"' in r.text and "<option selected>Br</option>" in r.text  # kept
        r = client.post("/investments/prices", data=data)
        assert 'class="msg error"' in r.text and "already has a price on 2026-01-07" in r.text
        body = {**data, "date": "2026-01-08", "price": 12}
        assert client.post("/api/investments/prices", json=body).status_code == 201
        assert client.post("/api/investments/prices", json=body).status_code == 422
        assert self.holding(settings) == (InvestmentType.STOCK, 120, 10)

    def test_add_price_creates_the_csv(self, settings):
        settings.prices_path.unlink()
        settings.investments_path.write_text("Br:\n  T: stock\n")
        LedgerStore(settings).add_price("2026-01-06", "Br", "T", "11")
        assert settings.prices_path.read_text() == "date;account;investment;price\n2026-01-06;Br;T;11.00\n"

    def test_free_sides_never_take_investment_accounts(self, settings, client):
        f = client.get("/form/invest-fields", params={"base_movement": "Investment - Bought"}).text
        credit = f.split('name="acc_credit"')[1].split("</select>")[0]
        assert "<option >Ca</option>" in credit and "<option >Br</option>" not in credit
        with pytest.raises(ValueError, match="credit account of 'Broker fees' cannot be an investment account: Br"):
            LedgerStore(settings).add("2026-02-01", "Broker fees", "Eq", "Br", "1", invest=True, investment="T")

    def test_no_form_without_invest_base_movements(self, settings, client):
        path = settings.base_movements_path
        path.write_text(path.read_text().replace(INVEST_YAML, "").replace(INVEST_TRANSFERS_YAML, ""))
        settings.investment_movements_path.unlink()
        assert 'id="invest-form"' not in client.get("/investments").text
