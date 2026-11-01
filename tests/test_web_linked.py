"""Tests for broker fees, investing in other currencies, cards in other currencies and the weeks without interest."""

import datetime as dt
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import (
    CARD_PAYMENT_YAML,
    INVEST_TRANSFERS_YAML,
    INVEST_YAML,
    with_conversion_bases,
    with_internal,
)

from pledger.web.app import create_app
from pledger.web.store import LedgerStore, row_fingerprint
from pledger.web.views import build_accounts, build_credit_cards, build_investments

BOUGHT, SOLD, PAY, SURPLUS = "Investment - Bought", "Investment - Sold", "Credit card - Payment", "Credit card - Surplus"
DIVIDENDS = "Dividends & Interest"


@pytest.fixture
def settings(revalued, tmp_path, monkeypatch):
    """With a broker, a USD and a EUR account, and a USD card. 1 USD is 1,000 and 1 EUR 1,200."""
    monkeypatch.setattr("pledger.web.store.common.today", lambda: dt.date(2026, 3, 11))
    settings = with_conversion_bases(revalued)
    with_internal(
        settings.base_movements_path, INVEST_YAML.replace("Dividends: d", f"{DIVIDENDS}: d").replace("id: 4", "id: 5"),
        INVEST_TRANSFERS_YAML + CARD_PAYMENT_YAML,
    )
    path = settings.accounts_path
    path.write_text(path.read_text(encoding="utf-8").replace("liability:\n  Cc:", (
        "  Br:\n    description: Broker\n"
        "  Ud:\n    description: Dollars\n    currency: USD\n    initial_amount: 500\n    initial_date: 2026-01-01\n"
        "  Eu:\n    description: Euros\n    currency: EUR\n    initial_amount: 500\n    initial_date: 2026-01-01\n"
        "liability:\n  Cc:"
    )), encoding="utf-8")
    folder = settings.currencies_path.parent
    (folder / "currencies.csv").write_text("date;USD;EUR\n2026-01-01;1000;1200\n", encoding="utf-8")
    yaml = settings.currencies_path
    yaml.write_text(yaml.read_text(encoding="utf-8") + "  - name: EUR\n    description: Euro\n", encoding="utf-8")
    investments = tmp_path / "investment.yaml"
    investments.write_text("Br:\n  T: stock\n", encoding="utf-8")
    cards = tmp_path / "credit_cards.yaml"
    cards.write_text(
        "Vd:\n  description: Visa\n  currency: USD\n  initial_amount: 100\n  initial_date: 2026-01-01\n", encoding="utf-8"
    )
    return replace(
        settings, investments_path=investments, credit_cards_path=cards,
        investment_movements_path=tmp_path / "investment.csv", conversions_path=tmp_path / "conversion.csv",
        interest_path=tmp_path / "interest.csv",
    )


@pytest.fixture
def store(settings):
    return LedgerStore(settings)


def table(rows, *columns):
    return [tuple(r[c] for c in columns) for r in rows.to_dict("records")]


SIDES = ("base_movement", "acc_debit", "amount", "acc_credit")
CONVERSION = ("base_movement", "acc_debit", "amount_base_currency", "acc_credit", "amount_other_currency")


class TestBrokerFees:
    def test_they_add_a_movement_out_of_the_same_account(self, store):
        store.add_investment_movement("2026-02-02", BOUGHT, "Br", "Ca", "1000", investment="T", quantity="10", fees="5")
        store.add_investment_movement("2026-02-03", SOLD, "Ca", "Br", "100", investment="T", quantity="1", fees="2")
        store.add_investment_movement("2026-02-04", DIVIDENDS, "Ca", "Eq", "50", investment="T", fees="1")
        assert table(store.load().investments, *SIDES, "investment") == [
            (BOUGHT, "Br", "1,000.00", "Ca", "T"), ("Broker fees", "Eq", "5.00", "Ca", "T"),
            (SOLD, "Ca", "100.00", "Br", "T"), ("Broker fees", "Eq", "2.00", "Ca", "T"),
            (DIVIDENDS, "Ca", "50.00", "Eq", "T"), ("Broker fees", "Eq", "1.00", "Ca", "T"),
        ]
        assert build_investments(store.load())["held"]["results"]["Broker fees"] == 8

    def test_only_those_base_movements_have_them(self, store):
        with pytest.raises(ValueError, match="can't add broker fees"):
            store.add_investment_movement("2026-02-02", "Broker fees", "Eq", "Ca", "1", investment="T", fees="5")

    def test_the_form_shows_them_and_adds_them(self, settings):
        client = TestClient(create_app(settings))
        assert 'name="fees"' in client.get("/form/invest-fields", params={"base_movement": BOUGHT}).text
        assert 'name="fees"' not in client.get("/form/invest-fields", params={"base_movement": "Broker fees"}).text
        r = client.post("/investments/movements", data={
            "move_date": "2026-02-02", "base_movement": BOUGHT, "acc_debit": "Br", "acc_credit": "Ca", "amount": "100",
            "investment": "T", "quantity": "1", "fees": "5", "currency": "ARS",
        })
        assert f"Added {BOUGHT} of T: $100.00" in r.text
        assert len(LedgerStore(settings).load().investments) == 2


class TestInOtherCurrencies:
    def test_buying_goes_through_fi_with_a_conversion(self, store):
        store.add_investment_movement(
            "2026-02-02", BOUGHT, "Br", "Ud", "100", investment="T", quantity="10", currency="USD", fees="1",
        )
        snapshot = store.load()
        assert table(snapshot.investments, *SIDES) == [
            (BOUGHT, "Br", "100,000.00", "Fi"), ("Broker fees", "Eq", "1,000.00", "Fi"),
        ]
        assert table(snapshot.conversions, *CONVERSION, "linked") == [
            ("Foreign currency - Sold", "Fi", "101,000.00", "Ud", "101.0000", "I-1 I-2"),
        ]
        assert snapshot.other_balances["Ud"] == pytest.approx(399)
        assert snapshot.ledger().balances()["Fi"] == 0

    def test_money_coming_in_is_converted_after_the_fees(self, store):
        store.add_investment_movement("2026-02-02", DIVIDENDS, "Ud", "Eq", "50", investment="T", currency="USD", fees="1")
        snapshot = store.load()
        assert table(snapshot.investments, *SIDES) == [
            (DIVIDENDS, "Fi", "50,000.00", "Eq"), ("Broker fees", "Eq", "1,000.00", "Fi"),
        ]
        assert table(snapshot.conversions, *CONVERSION) == [
            ("Foreign currency - Bought", "Ud", "49,000.00", "Fi", "49.0000"),
        ]
        assert snapshot.other_balances["Ud"] == pytest.approx(549)

    def test_an_amount_in_a_third_currency_is_valued_in_the_account_one(self, store):
        store.add_investment_movement("2026-02-02", BOUGHT, "Br", "Ud", "100", investment="T", quantity="10", currency="EUR")
        assert table(store.load().conversions, *CONVERSION) == [
            ("Foreign currency - Sold", "Fi", "120,000.00", "Ud", "120.0000"),
        ]

    def test_an_account_in_the_base_currency_needs_no_conversion(self, store):
        store.add_investment_movement("2026-02-02", BOUGHT, "Br", "Ca", "100", investment="T", quantity="10", currency="USD")
        snapshot = store.load()
        assert table(snapshot.investments, *SIDES) == [(BOUGHT, "Br", "100,000.00", "Ca")]
        assert len(snapshot.conversions) == 0

    def test_unknown_currency(self, store):
        with pytest.raises(ValueError, match="Unknown currency"):
            store.add_investment_movement("2026-02-02", BOUGHT, "Br", "Ud", "1", investment="T", quantity="1", currency="XXX")

    def test_fees_cant_take_it_all(self, store):
        with pytest.raises(ValueError, match="can't take up"):
            store.add_investment_movement("2026-02-02", DIVIDENDS, "Ud", "Eq", "5", investment="T", currency="USD", fees="5")
        assert len(store.load().investments) == 0

    def test_they_are_deleted_together_from_any_of_them(self, store):
        for delete in (
            lambda s: s.delete_investment(0),
            lambda s: s.delete_investment(1),
            lambda s: s.delete_conversion(0),
        ):
            store.add_investment_movement(
                "2026-02-02", BOUGHT, "Br", "Ud", "100", investment="T", quantity="10", currency="USD", fees="1",
            )
            delete(store)
            assert len(store.load().investments) == len(store.load().conversions) == 0

    def test_a_linked_amount_cant_be_edited(self, store):
        store.add_investment_movement("2026-02-02", BOUGHT, "Br", "Ud", "100", investment="T", quantity="10", currency="USD")
        with pytest.raises(ValueError, match="delete the movement"):
            store.edit_investment("investments", 0, None, "2026-02-02", BOUGHT, "Br", "Fi", "5", "", "T", "10")
        store.edit_investment("investments", 0, None, "2026-02-02", BOUGHT, "Br", "Fi", "100,000.00", "note", "T", "10")
        assert store.load().investments.iloc[0]["comment"] == "note"

    def test_the_form_offers_the_currencies(self, settings):
        client = TestClient(create_app(settings))
        page = client.get("/investments").text
        assert '<select name="currency">' in page and "data-account-currencies" in page
        r = client.post("/investments/movements", data={
            "move_date": "2026-02-02", "base_movement": BOUGHT, "acc_debit": "Br", "acc_credit": "Ud", "amount": "100",
            "investment": "T", "quantity": "1", "currency": "USD",
        })
        assert f"Added {BOUGHT} of T: 100.00 USD" in r.text and client.get("/investments").status_code == 200


class TestCardsInOtherCurrencies:
    def test_paying_from_its_currency_needs_one_amount(self, store):
        store.add_card_payment("2026-02-02", PAY, "Vd", "Ud", "40")
        snapshot = store.load()
        assert table(snapshot.rows[-1:], *SIDES, "amount_other_currency") == [(PAY, "Vd", "40,000.00", "Fi", "40.0000")]
        assert table(snapshot.conversions, *CONVERSION) == [("Foreign currency - Sold", "Fi", "40,000.00", "Ud", "40.0000")]
        assert snapshot.other_balances["Vd"] == pytest.approx(60)
        assert snapshot.other_balances["Ud"] == pytest.approx(460)

    def test_paying_in_the_base_currency_says_what_was_paid_in_the_card_one(self, store):
        with pytest.raises(ValueError, match="tell how much debt was paid in USD"):
            store.add_card_payment("2026-02-02", PAY, "Vd", "Ca", "45000")
        store.add_card_payment("2026-02-02", PAY, "Vd", "Ca", "45000", amount_card="40")
        snapshot = store.load()
        assert table(snapshot.rows[-1:], *SIDES, "amount_other_currency") == [(PAY, "Vd", "45,000.00", "Ca", "40.0000")]
        assert len(snapshot.conversions) == 0
        assert snapshot.other_balances["Vd"] == pytest.approx(60)  # exactly, whatever the value of the dollar

    def test_paying_in_a_third_currency_goes_through_fi(self, store):
        with pytest.raises(ValueError, match="tell how much"):
            store.add_card_payment("2026-02-02", PAY, "Vd", "Eu", "30")
        store.add_card_payment("2026-02-02", PAY, "Vd", "Eu", "30", amount_card="35")
        snapshot = store.load()
        assert table(snapshot.conversions, *CONVERSION) == [("Foreign currency - Sold", "Fi", "36,000.00", "Eu", "30.0000")]
        assert snapshot.other_balances["Vd"] == pytest.approx(65)
        assert snapshot.other_balances["Eu"] == pytest.approx(470)

    def test_a_surplus_comes_back(self, store):
        store.add_card_payment("2026-02-02", SURPLUS, "Ud", "Vd", "10")
        snapshot = store.load()
        assert table(snapshot.conversions, *CONVERSION) == [("Foreign currency - Bought", "Ud", "10,000.00", "Fi", "10.0000")]
        assert snapshot.other_balances["Vd"] == pytest.approx(110)

    def test_amounts_of_the_same_currency_must_match(self, store):
        with pytest.raises(ValueError, match="must match"):
            store.add_card_payment("2026-02-02", PAY, "Vd", "Ud", "40", amount_card="41")

    def test_add_alone_cant_skip_the_amount(self, store):
        with pytest.raises(ValueError, match="needs what was paid"):
            store.add("2026-02-02", PAY, "Vd", "Ca", "10", card_payment=True)

    def test_a_ledger_row_cant_have_the_amount_elsewhere(self, settings, store):
        path = settings.ledger_path
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[0] += ";amount_other_currency"
        lines[1] += ";5"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(Exception, match="Only the card payments of a credit card in another currency"):
            store.load()

    def test_deleting_takes_its_conversion(self, store):
        n = len(store.load().rows)
        store.add_card_payment("2026-02-02", PAY, "Vd", "Ud", "40")
        store.delete(n, card_payment=True)
        assert len(store.load().rows) == n and len(store.load().conversions) == 0

    def test_editing(self, store):
        store.add_card_payment("2026-02-02", PAY, "Vd", "Ca", "45000", amount_card="40")
        n = len(store.load().rows) - 1
        with pytest.raises(ValueError, match="tell how much"):
            store.edit_card_payment(n, None, "2026-02-02", PAY, "Vd", "Ca", "45000")
        store.edit_card_payment(n, None, "2026-02-02", PAY, "Vd", "Ca", "46000", amount_card="41")
        assert table(store.load().rows[-1:], "amount", "amount_other_currency") == [("46,000.00", "41.0000")]

    def test_a_linked_amount_cant_be_edited(self, store):
        store.add_card_payment("2026-02-02", PAY, "Vd", "Ud", "40")
        n = len(store.load().rows) - 1
        with pytest.raises(ValueError, match="delete the payment"):
            store.edit_card_payment(n, None, "2026-02-02", PAY, "Vd", "Fi", "1", amount_card="40")
        store.edit_card_payment(n, None, "2026-02-02", PAY, "Vd", "Fi", "40,000.00", amount_card="39")
        assert store.load().other_balances["Vd"] == pytest.approx(61)

    def test_the_tab_shows_the_debt_in_its_currency(self, settings, store):
        store.add_card_payment("2026-02-02", PAY, "Vd", "Ca", "45000", amount_card="40")
        data = build_credit_cards(store.load())
        assert data["has_own"] and data["accounts"][0]["own"] == pytest.approx(60)
        assert build_credit_cards(store.load(), "2026-01")["accounts"][0]["own"] == pytest.approx(100)
        client = TestClient(create_app(settings))
        page = client.get("/credit-cards", params={"month": ""}).text
        assert "60.00 USD" in page and 'name="amount_card"' in page

    def test_the_form_pays_and_edits(self, settings, store):
        client = TestClient(create_app(settings))
        data = {"move_date": "2026-02-02", "base_movement": PAY, "acc_debit": "Vd", "acc_credit": "Ca", "amount": "45,000"}
        assert "tell how much debt" in client.post("/credit-cards/payments", data=data).text
        r = client.post("/credit-cards/payments", data={**data, "amount_card": "40"})
        assert "Paid Vd: 45,000.00 ARS" in r.text
        snapshot = store.load()
        n, row = len(snapshot.rows) - 1, snapshot.rows.iloc[-1]
        form = client.get(f"/credit-cards/payments/{n}/edit", params={"fingerprint": row_fingerprint(row)}).text
        assert 'value="40.0000"' in form
        r = client.post(f"/credit-cards/payments/{n}/edit", data={**data, "fingerprint": row_fingerprint(row), "amount_card": "41"})
        assert store.load().rows.iloc[-1]["amount_other_currency"] == "41.0000"

    def test_the_api_needs_the_amount(self, settings):
        client = TestClient(create_app(settings))
        body = {"move_date": "2026-02-02", "base_movement": PAY, "acc_debit": "Vd", "acc_credit": "Ca", "amount": 45000}
        assert client.post("/api/credit-cards/payments", json=body).status_code == 422
        assert client.post("/api/credit-cards/payments", json={**body, "amount_card": 40}).status_code == 201

    def test_other_cards_arent_asked(self, settings, store):
        path = settings.credit_cards_path
        path.write_text("Vi:\n  description: Visa\n  initial_amount: 100\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Only a card in another currency"):
            store.add_card_payment("2026-02-02", PAY, "Vi", "Ca", "10", amount_card="1")
        store.add_card_payment("2026-02-02", PAY, "Vi", "Ca", "10")
        assert 'name="amount_card"' not in TestClient(create_app(settings)).get("/credit-cards").text


class TestMissingInterest:
    @pytest.fixture
    def settings(self, settings):
        with_internal(settings.base_movements_path, (
            "    Interest:\n      id: 6\n      long_name: I\n      description: d\n      base_movements:\n"
            "        gain:\n          Interest on Money (G): d\n        loss:\n          Interest on Money (L): d\n"
        ))
        return settings

    @staticmethod
    def write(settings, text):
        settings.interest_path.write_text(f"move_date;acc_debit;amount;comment\n{text}", encoding="utf-8")

    def test_past_weeks_without_interest_warn(self, settings):
        # Today is Wednesday 2026-03-11: the week of 03-02 has passed, that of 03-09 hasn't
        self.write(settings, "2026-02-16;Ca;1;\n2026-02-25;Ca;1;\n2026-03-01;Ud;1;\n")
        data = build_investments(LedgerStore(settings).load())
        assert data["missing_interest"] == [{"name": "Ca", "weeks": ["2026-03-02"]}, {"name": "Ud", "weeks": ["2026-03-02"]}]
        page = TestClient(create_app(settings)).get("/investments").text
        assert "2 accounts have past weeks without interest" in page
        assert "<strong>Ca</strong> (1): 2026-03-02" in page

    def test_the_weeks_before_the_first_one_and_the_current_one_dont(self, settings):
        self.write(settings, "2026-03-02;Ca;1;\n")
        assert build_investments(LedgerStore(settings).load())["missing_interest"] == []
        assert "without interest" not in TestClient(create_app(settings)).get("/investments").text


class TestAccountsByCurrency:
    @staticmethod
    def groups(text, side):
        select = text.split(f'name="{side}"')[1].split("</select>")[0]
        return [g.split('"')[0] for g in select.split('<optgroup label="')[1:]]

    def test_debit_and_credit_are_separated(self, settings):
        client = TestClient(create_app(settings))
        text = client.get("/form/accounts", params={"base_movement": "Move"}).text
        for side in ("acc_debit", "acc_credit"):
            assert self.groups(text, side) == ["ARS", "USD", "EUR"]
        select = text.split('name="acc_debit"')[1].split("</select>")[0]
        assert "<option >Ud</option>" in select.split('label="USD"')[1].split("</optgroup>")[0]

    def test_a_single_currency_has_no_groups(self, settings):
        text = TestClient(create_app(settings)).get(f"/form/accounts?base_movement={SURPLUS}").text
        assert self.groups(text, "acc_credit") == []  # only Vd, in USD

    def test_the_selected_one_stays(self, settings):
        client = TestClient(create_app(settings))
        data = {"move_date": "2026-02-02", "base_movement": "Move", "acc_debit": "Ud", "acc_credit": "Ca", "amount": "-1"}
        select = client.post("/movements", data=data).text.split('name="acc_debit"')[1].split("</select>")[0]
        assert "<option selected>Ud</option>" in select


class TestFeesField:
    def test_it_comes_after_the_amount_and_the_currency(self, settings):
        form = TestClient(create_app(settings)).post("/investments/movements", data={
            "move_date": "2026-02-02", "base_movement": BOUGHT, "move_type": "transfer", "amount": "-1", "fees": "5",
        }).text
        assert form.index('name="amount"') < form.index('name="currency"') < form.index('name="fees"')
        assert 'name="fees" inputmode="decimal" value="5"' in form

    def test_the_base_movement_updates_it_out_of_band(self, settings):
        client = TestClient(create_app(settings))
        for url in (f"/form/invest-fields?base_movement={BOUGHT}", "/form/invest-bases?move_type=transfer"):
            assert 'id="invest-fees" class="stack" hx-swap-oob="true"' in client.get(url).text
        text = client.get("/form/invest-fields", params={"base_movement": "Broker fees"}).text
        assert 'hx-swap-oob="true"></div>' in text and 'name="fees"' not in text


class TestLedgerInOtherCurrencies:
    def test_a_loss_is_in_the_currency_of_its_account(self, store):
        n = len(store.load().rows)
        store.add_ledger_movement("2026-02-02", "Food", "Eq", "Ud", "10", "Lunch")
        snapshot = store.load()
        assert table(snapshot.rows[n:], *SIDES, "comment") == [("Food", "Eq", "10,000.00", "Fi", "Lunch")]
        assert table(snapshot.conversions, *CONVERSION, "comment") == [
            ("Foreign currency - Sold", "Fi", "10,000.00", "Ud", "10.0000", "Lunch"),
        ]
        assert snapshot.other_balances["Ud"] == pytest.approx(490)
        assert snapshot.ledger().balances()["Fi"] == 0

    def test_a_gain_is_too(self, store):
        n = len(store.load().rows)
        store.add_ledger_movement("2026-02-02", "Gift", "Eu", "Eq", "10")
        snapshot = store.load()
        assert table(snapshot.rows[n:], *SIDES) == [("Gift", "Fi", "12,000.00", "Eq")]
        assert table(snapshot.conversions, *CONVERSION) == [("Foreign currency - Bought", "Eu", "12,000.00", "Fi", "10.0000")]
        assert snapshot.other_balances["Eu"] == pytest.approx(510)

    def test_a_charge_to_a_card_in_another_currency(self, store):
        store.add_ledger_movement("2026-02-02", "Food", "Eq", "Vd", "12.34")
        snapshot = store.load()
        assert table(snapshot.conversions, *CONVERSION) == [("Foreign currency - Sold", "Fi", "12,340.00", "Vd", "12.3400")]
        assert snapshot.other_balances["Vd"] == pytest.approx(112.34)

    def test_the_base_currency_and_transfers_stay_as_they_are(self, store):
        n = len(store.load().rows)
        store.add_ledger_movement("2026-02-02", "Food", "Eq", "Ca", "10")
        store.add_ledger_movement("2026-02-02", "Move", "Ud", "Ca", "1000")
        assert table(store.load().rows[n:], *SIDES) == [("Food", "Eq", "10.00", "Ca"), ("Move", "Ud", "1,000.00", "Ca")]
        assert len(store.load().conversions) == 0

    def test_a_fixed_account_cant_go_through_fi(self, store):
        n = len(store.load().rows)
        store.add_ledger_movement("2026-02-02", "Salary", "Bk", "Eq", "2")
        snapshot = store.load()
        assert table(snapshot.rows[n:], *SIDES) == [("Salary", "Bk", "2,000.00", "Eq")]
        assert len(snapshot.conversions) == 0

    def test_a_movement_that_doesnt_hold_adds_nothing(self, store):
        n = len(store.load().rows)
        with pytest.raises(ValueError, match="cannot be negative"):
            store.add_ledger_movement("2026-02-02", "Food", "Eq", "Ud", "-1")
        with pytest.raises(ValueError, match="Unknown base movement"):
            store.add_ledger_movement("2026-02-02", "Nope", "Eq", "Ud", "1")
        assert len(store.load().rows) == n and len(store.load().conversions) == 0

    def test_they_are_deleted_together(self, store):
        n = len(store.load().rows)
        store.add_ledger_movement("2026-02-02", "Food", "Eq", "Ud", "10")
        store.delete(n)
        assert len(store.load().rows) == n and len(store.load().conversions) == 0
        store.add_ledger_movement("2026-02-02", "Food", "Eq", "Ud", "10")
        store.delete_conversion(0)
        assert len(store.load().rows) == n and len(store.load().conversions) == 0

    def test_editing_keeps_its_date_and_amount(self, store):
        n = len(store.load().rows)
        store.add_ledger_movement("2026-02-02", "Food", "Eq", "Ud", "10")
        for date, amount in (("2026-02-03", "10,000.00"), ("2026-02-02", "5")):
            with pytest.raises(ValueError, match="delete the movement"):
                store.edit(n, None, date, "Food", "Eq", "Fi", amount)
        store.edit(n, None, "2026-02-02", "Food", "Eq", "Fi", "10,000.00", "Dinner")
        assert store.load().rows.iloc[n]["comment"] == "Dinner"

    def test_the_form_and_the_api(self, settings, store):
        client = TestClient(create_app(settings))
        data = {"move_date": "2026-02-02", "move_type": "loss", "base_movement": "Food", "acc_debit": "Eq", "acc_credit": "Ud", "amount": "10"}
        assert "Added Food: 10.00 USD" in client.post("/movements", data=data).text
        assert client.post("/api/movements", json={k: v for k, v in data.items() if k != "move_type"}).status_code == 201
        assert len(store.load().conversions) == 2
        page = client.get("/").text
        assert "data-foreign-accounts" in page and "data-amount-prefix" in page

    def test_copying_gives_the_account_and_the_amount_back(self, settings, store):
        n = len(store.load().rows)
        store.freeze_ids()
        store.add_ledger_movement("2026-02-02", "Food", "Eq", "Ud", "10.50")
        row = store.load().rows.iloc[n]
        form = TestClient(create_app(settings)).get(f"/movements/{n}/copy", params={"fingerprint": row_fingerprint(row)}).text
        assert 'value="10.5000"' in form and "<option selected>Ud</option>" in form.split('name="acc_credit"')[1]

    def test_copying_a_direct_row_gives_its_currency(self, settings, store):
        with settings.ledger_path.open("a", encoding="utf-8") as f:
            f.write("2026-02-02;Food;Eq;5000;Ud;\n")
        n = len(store.load().rows) - 1
        row = store.load().rows.iloc[n]
        form = TestClient(create_app(settings)).get(f"/movements/{n}/copy", params={"fingerprint": row_fingerprint(row)}).text
        assert 'name="amount" inputmode="decimal" min="0" value="5.00"' in form


class TestBalanceInItsCurrency:
    def test_accounts_in_other_currencies_have_it(self, settings, store):
        store.add_ledger_movement("2026-02-02", "Food", "Eq", "Ud", "10")
        data = build_accounts(store.load())
        assets = {a["name"]: a["own"] for a in data["accounts_by_type"]["asset"]}
        assert assets["Ud"] == pytest.approx(490) and assets["Eu"] == pytest.approx(500)
        assert assets["Ca"] is None and assets["Br"] is None  # the investment ones follow the base currency
        assert data["has_own"]["asset"] and data["has_own"]["liability"] and not data["has_own"]["equity"]
        assert {a["name"]: a["own"] for a in data["accounts_by_type"]["liability"]}["Vd"] == pytest.approx(100)

    def test_as_of_the_month_picked(self, store):
        store.add_ledger_movement("2026-02-02", "Food", "Eq", "Ud", "10")
        assets = {a["name"]: a["own"] for a in build_accounts(store.load(), "2026-01")["accounts_by_type"]["asset"]}
        assert assets["Ud"] == pytest.approx(500)

    def test_the_tab_shows_it(self, settings):
        table_html = TestClient(create_app(settings)).get("/accounts", params={"month": ""}).text
        asset = table_html.split('data-sortable="balances-asset"')[1].split("</table>")[0]
        equity = table_html.split('data-sortable="balances-equity"')[1].split("</table>")[0]
        assert "Balance in its currency" in asset and ">500.00</td>" in asset
        assert "Balance in its currency" not in equity
