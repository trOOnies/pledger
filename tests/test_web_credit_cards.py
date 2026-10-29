"""Tests for the Credit Cards tab."""

import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import (
    CARD_PAYMENT_YAML,
    CREDIT_CARDS_YAML,
    HTMX,
    card_labels,
    movement_ids,
    rows,
    with_internal,
)

from pledger.models import CreditCardAccount
from pledger.web.app import create_app
from pledger.web.store import (
    LedgerDataError,
    LedgerStore,
    row_fingerprint,
)
from pledger.web.views import (
    build_credit_cards,
)


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
        with pytest.raises(LedgerDataError, match=re.escape("only has the creditors of De, not of ['Vi']")):
            LedgerStore(replace(settings, creditors_path=creditors)).load()

    @pytest.fixture
    def paying(self, settings):
        """With the internal card payment base movement, debiting any credit card."""
        path = settings.base_movements_path
        with_internal(path, transfers=CARD_PAYMENT_YAML)
        return settings

    def test_card_payments_are_added_from_their_tab(self, paying):
        client = TestClient(create_app(paying))
        pay = "Credit card - Payment"
        assert LedgerStore(paying).load().card_payment_base_movements().keys() == {pay, "Credit card - Surplus"}
        with pytest.raises(ValueError, match="'Credit card - Payment' are added from the Credit Cards tab"):
            LedgerStore(paying).add("2026-02-07", pay, "Vi", "Bk", "10")
        assert pay not in client.get("/form/bases", params={"move_type": "transfer"}).text
        r = client.get("/credit-cards", params={"month": ""})
        form = r.text.split('id="card-payment-form"')[1].split("</form>")[0]
        # The base movement is picked (a payment by default), and the debit is any credit card
        assert f'<label class="liability" title="{pay}">\n        <input type="radio" name="base_movement" value="{pay}" checked' in form
        assert '<label class="asset" title="Credit card - Surplus">' in form  # orange and blue buttons
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
        pay = "Credit card - Payment"
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

    def test_card_surplus(self, paying):
        client = TestClient(create_app(paying))
        surplus = "Credit card - Surplus"
        form = client.get("/form/card-payment", params={"base_movement": surplus}).text
        assert f'value="{surplus}" checked' in form
        fields = client.get("/form/accounts", params={"base_movement": surplus}).text  # picking its button
        assert fields == form.split('<div id="card-account-fields">')[1].split("</div>\n  <label>Amount")[0]
        credit = fields.split('name="acc_credit"')[1].split("</select>")[0]
        assert "<option >Vi</option>" in credit and "Bk" not in credit  # any credit card
        r = client.post("/credit-cards/payments", data={
            "move_date": "2026-02-07", "base_movement": surplus, "acc_debit": "Bk", "acc_credit": "Vi", "amount": "10",
        })
        assert "Surplus of Vi: $10.00" in r.text
        assert rows(paying).iloc[-1]["base_movement"] == surplus

    def test_credit_cards_tab_without_card_payment_base_movements(self, client):
        assert "The Credit Cards tab needs these base movements" in client.get("/credit-cards").text

    def test_credit_cards_tab(self, client):
        r = client.get("/credit-cards", params={"month": ""})
        assert r.status_code == 200
        assert 'href="/credit-cards" aria-current="page"' in r.text
        assert card_labels(r.text) == ["Charged", "Paid", "Owed"]
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
