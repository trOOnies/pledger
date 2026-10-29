"""Tests for the Currencies tab: currencies, inflation and conversions."""

import datetime as dt
import re
from dataclasses import replace

import pytest
from conftest import RESULT_YAML
from fastapi.testclient import TestClient
from helpers import HTMX, movement_ids, with_conversion_bases

from pledger.web.app import create_app
from pledger.web.store import (
    CROSS_CONVERSION,
    LedgerDataError,
    LedgerStore,
    Settings,
    StaleRowError,
    conversion_fingerprint,
    conversion_pairs,
)
from pledger.web.views import (
    build_conversions,
    build_currencies,
    column_chart,
    conversion_filter,
    percent,
)


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
            "name": "ARS", "description": "Pesos", "accounts": 4,  # Ca, Cc, Fi and De
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
        # Without values of USD, an initial amount in USD can't be valued
        path = settings.accounts_path
        path.write_text(path.read_text().replace("initial_amount: 0.1005", "initial_amount: 0"))
        (folder / "currencies.csv").write_text("date;USD\n")
        (folder / "base_currency.csv").write_text("month;monthly_inflation\n")
        r = client.get("/currencies")
        assert r.status_code == 200
        assert "No months in <code>base_currency.csv</code> yet." in r.text
        assert "No values of USD in <code>currencies.csv</code> yet." in r.text


class TestConversions:
    HEADER = "move_date;base_movement;acc_debit;amount_base_currency;acc_credit;comment;amount_other_currency\n"

    @pytest.fixture
    def conv(self, revalued, tmp_path) -> Settings:
        """With a conversion: 1 USD bought for 1,100 ARS on 2026-01-05, and a USD value after it."""
        with_conversion_bases(revalued)
        path = tmp_path / "movements" / "conversion.csv"
        path.parent.mkdir()
        path.write_text(self.HEADER + "2026-01-05;Foreign currency - Bought;Bk;1,100.00;Ca;Bought;1.0000\n", encoding="utf-8")
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
        assert self.rows(snapshot.conversion_rows) == [(-2, "2026-01-05", "Foreign currency - Bought", "Bk", "1,100.00", "Ca", "Bought")]
        dates = list(snapshot.all_rows.move_date)
        assert dates == sorted(dates)  # in date order
        balances = snapshot.ledger().balances()
        assert balances["Ca"] == pytest.approx(159.75 - 1100)

    def test_accounts_in_other_currencies_are_revalued(self, conv):
        # Bk: 0.1005 USD initially (at the first value), 1.0005 of salary, 0.2 withdrawn and 1 bought:
        # 1.901 USD, 2,001 ARS in the ledger, worth 2,281.20 at 1,200
        snapshot = LedgerStore(conv).load()
        assert self.rows(snapshot.result_rows) == [
            (-3, "2026-01-10", "Foreign currency (G)", "Bk", "280.20", "Eq", "1.9010 USD at 1,200.0000"),
        ]
        assert snapshot.ledger().balances()["Bk"] == pytest.approx(2281.2)
        assert snapshot.other_balances == {"Bk": pytest.approx(1.901)}

    def test_a_lower_value_is_a_loss(self, conv, tmp_path):
        (tmp_path / "currencies.csv").write_text("date;USD\n2026-01-01;1000\n2026-01-10;900\n2026-01-20;900\n")
        snapshot = LedgerStore(conv).load()
        assert self.rows(snapshot.result_rows) == [  # nothing more on 2026-01-20
            (-3, "2026-01-10", "Foreign currency (L)", "Eq", "290.10", "Bk", "1.9010 USD at 900.0000"),
        ]
        assert snapshot.ledger().balances()["Bk"] == pytest.approx(1710.9)

    def test_the_result_base_movements_are_required_to_revalue(self, conv):
        conv.base_movements_path.write_text(conv.base_movements_path.read_text().replace(RESULT_YAML, ""))
        with pytest.raises(LedgerDataError, match=re.escape("'Foreign currency (G)' is required to revalue")):
            LedgerStore(conv).load()

    def test_results_cannot_be_in_the_ledger_csv(self, conv):
        with conv.ledger_path.open("a") as f:
            f.write("2026-01-05;Foreign currency (G);Bk;5;Eq;\n")
        with pytest.raises(LedgerDataError, match=re.escape("cannot have movements of ['Foreign currency (G)']")):
            LedgerStore(conv).load()

    @pytest.mark.parametrize(
        ("row", "error"),
        [
            ("2026-01-05;Foreign currency - Bought;Cc;100;Ca;;1", "between an account in ARS and one in another currency, not Cc and Ca"),
            ("2026-01-05;Gift;Bk;100;Eq;;1", "must be of 'Foreign currency - Bought' or 'Foreign currency - Sold' (internal transfers), not 'Gift'"),
            ("2026-01-05;Nope;Bk;100;Ca;;1", "Unknown base movement of a conversion: 'Nope'"),
            ("2026-13-05;Foreign currency - Bought;Bk;100;Ca;;1", "Invalid date of a conversion"),
            ("2026-01-05;Foreign currency - Bought;Bk;100;Ca;;x", "Invalid amount in USD of a conversion"),
            ("2026-01-05;Foreign currency - Bought;Bk;100;Ca;;0", "must both be zero or both positive"),
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

    def test_filter_by_currency(self, conv, conv_client):
        r = conv_client.get("/currencies")
        assert '<form id="conversion-filter"' in r.text and "<option >USD</option>" in r.text
        assert 'hx-include="#conversion-filter"' in r.text
        assert "<h2>Conversions <small>1</small></h2>" in conv_client.get("/conversions", params={"currency": "USD"}).text
        assert "<h2>Conversions <small>1</small></h2>" in conv_client.get("/conversions", params={"currency": "Nope"}).text
        snapshot = LedgerStore(conv).load()
        assert build_conversions(snapshot, "EUR") == [] and len(build_conversions(snapshot, "USD")) == 1
        assert conversion_filter(snapshot, "ARS")["conversion_currency"] == ""  # the base one isn't picked

    def test_account_filter_and_balance_in_the_other_currency(self, conv, conv_client):
        snapshot = LedgerStore(conv).load()
        (c,) = build_conversions(snapshot)
        # Bk: 0.901 USD before buying 1 more
        assert c["account"] == "Bk" and c["other_balance"] == pytest.approx(1.901)
        assert conversion_filter(snapshot, "USD")["conversion_accounts"] == ["Bk"]
        assert conversion_filter(snapshot, "USD", "Ca")["conversion_account"] == ""  # not in USD
        r = conv_client.get("/conversions", params={"currency": "USD", "account": "Bk"})
        assert "<h2>Conversions <small>1</small></h2>" in r.text and "<option selected>Bk</option>" in r.text
        assert 'hx-swap-oob="true"' in r.text and "USD 1.9010</td>" in r.text

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
        store.add_conversion("2026-01-06", "Foreign currency - Sold", "Ca", "Bk", "1234.5", "1.2", "Sold; half")
        assert conv.conversions_path.read_text().splitlines()[-1] == (
            "2026-01-06;Foreign currency - Sold;Ca;1,234.50;Bk;Sold, half;1.2000;C-2"
        )
        conversions = store.load().conversions
        assert len(conversions) == 2
        with pytest.raises(StaleRowError):
            store.delete_conversion(1, "2026-01-06|Foreign currency - Sold|1,234.50|9.0000")
        store.delete_conversion(1, conversion_fingerprint(conversions.iloc[1]))
        assert conv.conversions_path.read_text() == (
            self.HEADER.replace("\n", ";id\n") + "2026-01-05;Foreign currency - Bought;Bk;1,100.00;Ca;Bought;1.0000;C-1\n"
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
        assert "Updated C-1 Foreign currency - Bought: 1,200.00 ARS sold for 1.00 USD" in r.text
        assert r.headers["HX-Trigger"] == "conversions-changed"
        assert conv.conversions_path.read_text().splitlines()[-1] == "2026-01-05;Foreign currency - Bought;Bk;1,200.00;Ca;Bought;1.0000;C-1"

    def test_adding_creates_the_csv(self, revalued, tmp_path):
        path = tmp_path / "movements" / "conversion.csv"
        store = LedgerStore(replace(with_conversion_bases(revalued), conversions_path=path))
        store.add_conversion("2026-01-06", "Foreign currency - Bought", "Bk", "Ca", "100", "0.1")
        assert path.read_text() == (
            self.HEADER.replace("\n", ";id\n") + "2026-01-06;Foreign currency - Bought;Bk;100.00;Ca;;0.1000;C-1\n"
        )

    @pytest.mark.parametrize(
        ("args", "error"),
        [
            (("2026-01-06", "Foreign currency - Bought", "Ca", "Cc", "100", "1"), "between an account in ARS and one in another currency"),
            (("2026-01-06", "Foreign currency - Bought", "Bk", "Bk", "100", "1"), "must be different"),
            (("2026-01-06", "Foreign currency - Bought", "Bk", "Ca", "0", "0"), "must be positive"),
            (("2026-01-06", "Foreign currency - Bought", "Bk", "Ca", "-1", "1"), "cannot be negative"),
            (("2026-01-06", "Move", "Bk", "Ca", "100", "1"), "must be of 'Foreign currency - Bought' or 'Foreign currency - Sold', not 'Move'"),
            (("2026-01-06", "Foreign currency - Sold", "Bk", "Ca", "100", "1"), "into Bk is a 'Foreign currency - Bought' conversion"),
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
        assert store.add_exchange("2026-01-06", "USD", "ARS", "Bk", "Ca", "0.5", "600") == "Foreign currency - Sold"
        assert conv.conversions_path.read_text().splitlines()[-1] == "2026-01-06;Foreign currency - Sold;Ca;600.00;Bk;;0.5000;C-2"
        for args, error in [
            (("USD", "USD", "Bk", "Bk"), "sell or buy ARS for another currency, or one other currency for another, not USD for USD"),
            (("ARS", "EUR", "Ca", "Bk"), "Unknown currency: 'EUR'"),
            (("ARS", "USD", "Bk", "Ca"), "The account from must be in ARS, but Bk is in USD"),
        ]:
            with pytest.raises(ValueError, match=re.escape(error)):
                store.add_exchange("2026-01-06", *args, "1", "1")

    @pytest.fixture
    def cross(self, conv) -> Settings:
        """With EUR and an Eu account in it."""
        conv.currencies_path.write_text(conv.currencies_path.read_text() + "  - name: EUR\n    description: Euro\n")
        (conv.currencies_path.parent / "currencies.csv").write_text(
            "date;USD;EUR\n2026-01-01;1000;1100\n2026-01-10;1200;1300\n"
        )
        extra = "  Eu:\n    description: Euros\n    currency: EUR\n    initial_amount: 0\n"
        conv.accounts_path.write_text(conv.accounts_path.read_text().replace("liability:\n", extra + "liability:\n", 1))
        return conv

    def test_cross_conversions_go_through_an_intermediary_account(self, cross):
        store = LedgerStore(cross)
        # 0.5 USD at 1,000 on 2026-01-06: 500.00 ARS into Fi and out of it
        assert store.add_exchange("2026-01-06", "USD", "EUR", "Bk", "Eu", "0.5", "0.4", "Swap") == CROSS_CONVERSION
        assert cross.conversions_path.read_text().splitlines()[-2:] == [
            "2026-01-06;Foreign currency - Sold;Fi;500.00;Bk;Swap;0.5000;C-2",
            "2026-01-06;Foreign currency - Bought;Eu;500.00;Fi;Swap;0.4000;C-3",
        ]
        snapshot = store.load()
        assert conversion_pairs(snapshot) == {1: 2, 2: 1}
        assert snapshot.other_balances["Eu"] == pytest.approx(0.4)
        assert snapshot.ledger().balances().get("Fi", 0.0) == pytest.approx(0.0)
        listed = {c["id"]: (c["pair_id"], c["through"]) for c in build_conversions(snapshot)}
        assert listed == {"C-1": (None, "Ca"), "C-2": ("C-3", "Fi"), "C-3": ("C-2", "Fi")}

    def test_cross_conversions_are_edited_and_deleted_together(self, cross):
        store = LedgerStore(cross)
        store.add_exchange("2026-01-06", "USD", "EUR", "Bk", "Eu", "0.5", "0.4")
        fingerprint = conversion_fingerprint(store.load().conversions.iloc[2])
        assert store.edit_exchange(2, fingerprint, "2026-01-07", "USD", "EUR", "Bk", "Eu", "0.25", "0.2", "Less") == CROSS_CONVERSION
        assert cross.conversions_path.read_text().splitlines()[-2:] == [
            "2026-01-07;Foreign currency - Sold;Fi;250.00;Bk;Less;0.2500;C-2",
            "2026-01-07;Foreign currency - Bought;Eu;250.00;Fi;Less;0.2000;C-3",
        ]
        snapshot = store.load()
        with pytest.raises(ValueError, match="between two other currencies and stays one"):
            store.edit_exchange(1, None, "2026-01-07", "USD", "ARS", "Bk", "Ca", "0.25", "250")
        with pytest.raises(ValueError, match="with ARS and stays one"):
            store.edit_exchange(0, None, "2026-01-07", "USD", "EUR", "Bk", "Eu", "0.25", "0.2")
        store.delete_conversion(1, conversion_fingerprint(snapshot.conversions.iloc[1]))
        assert len(store.load().conversions) == 1

    def test_cross_conversion_form(self, cross):
        client = TestClient(create_app(cross))
        r = client.get("/form/conversion-fields", params={"sold": "USD", "bought": "EUR"})
        assert 'name="through"' not in r.text and "conversion of\n  USD into Fi" in r.text  # always through Fi
        r = client.post("/conversions", data={
            "move_date": "2026-01-06", "sold": "USD", "bought": "EUR", "acc_from": "Bk", "acc_to": "Eu",
            "amount_sold": "0.5", "amount_bought": "0.4",
        })
        assert f"Added {CROSS_CONVERSION}: 0.50 USD sold for 0.40 EUR" in r.text
        row = LedgerStore(cross).load().conversions.iloc[2]
        form = client.get("/conversions/2/edit", params={"fingerprint": conversion_fingerprint(row)}, headers=HTMX).text
        assert '<option selected>Eu</option>' in form and 'name="amount_sold" inputmode="decimal" min="0" value="0.5000"' in form
        assert "⇄ C-3" in client.get("/currencies").text

    def test_add_exchange_needs_the_base_movement(self, revalued, tmp_path):
        store = LedgerStore(replace(revalued, conversions_path=tmp_path / "conversion.csv"))
        with pytest.raises(ValueError, match=re.escape("No 'Foreign currency - Bought' base movement: add it as an internal transfer")):
            store.add_exchange("2026-01-06", "ARS", "USD", "Ca", "Bk", "1000", "1")

    def test_conversion_fields_follow_the_currencies(self, conv_client):
        def fields(**params):
            r = conv_client.get("/form/conversion-fields", params=params, headers=HTMX)
            sold = r.text.split('name="sold"')[1].split("</select>")[0]
            bought = r.text.split('name="bought"')[1].split("</select>")[0]
            return re.search(r"<option selected>(\w+)", sold)[1], re.search(r"<option selected>(\w+)", bought)[1], r.text

        assert fields()[:2] == ("ARS", "USD")  # those of the last conversion
        sold, bought, text = fields(sold="USD", bought="USD", changed="sold")
        assert (sold, bought) == ("USD", "ARS") and "Foreign currency - Sold" in text
        assert '>Bk</option>' in text.split('name="acc_from"')[1].split("</select>")[0]
        assert fields(sold="ARS", bought="ARS", changed="bought")[:2] == ("USD", "ARS")

    def test_not_in_the_ledger_tab(self, conv, conv_client):
        form = conv_client.get("/").text.split('id="movement-form"')[1].split("</form>")[0]
        assert "Foreign currency - Bought" not in form and "Foreign currency - Sold" not in form
        with pytest.raises(ValueError, match="are added from the Currencies tab"):
            LedgerStore(conv).add("2026-01-06", "Foreign currency - Bought", "Bk", "Ca", "100")

    def test_ledger_tab_links_them_to_the_currencies_tab(self, conv_client):
        r = conv_client.get("/")
        assert '<a class="source" href="/currencies" title="Added, edited and deleted from the Currencies tab"' in r.text
        assert "🔵</a>" in r.text
        assert r.text.count('hx-post="/movements/') == 4  # only the CSV rows
        assert "Foreign currency (G)" in r.text  # internal: not deletable either
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
        assert "Added Foreign currency - Bought: 1,000.00 ARS sold for 1.00 USD" in r.text
        assert r.headers["HX-Trigger"] == "conversions-changed"
        assert "2026-01-06;Foreign currency - Bought;Bk;1,000.00;Ca;;1.0000" in conv.conversions_path.read_text()
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
        body = {"move_date": "2026-01-06", "base_movement": "Foreign currency - Sold", "acc_debit": "Ca", "acc_credit": "Bk",
                "amount_base": 500, "amount_other": 0.5}
        assert conv_client.post("/api/conversions", json=body).status_code == 201
        assert conv_client.post("/api/conversions", json={**body, "acc_credit": "Cc"}).status_code == 422
        (first, _) = conv_client.get("/api/conversions").json()
        assert conv_client.delete(f"/api/conversions/{first['index']}", params={"fingerprint": first["fingerprint"]}).status_code == 200
