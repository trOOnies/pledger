import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import CREDIT_CARDS_YAML

from pledger.web.app import create_app
from pledger.web.store import DASHBOARD_SLOTS, DEFAULT_DASHBOARD
from pledger.web.views import WIDGETS


def slots(html: str, kind: str) -> list[str | None]:
    """The widget of each slot of a kind, None for a free one, in order."""
    grid = html.split(f'data-kind="{kind}">', 1)[1].split('<div class="widget-grid', 1)[0]
    found = re.findall(r'<article class="widget[^"]*" data-kind="\w+" data-slot="(\d+)" data-widget="([\w-]+)"|<button type="button" class="widget-free" data-kind="\w+" data-slot="(\d+)"', grid)
    return [widget or None for _, widget, _ in found]


def default(kind: str) -> list[str | None]:
    return (DEFAULT_DASHBOARD[kind] + [None] * DASHBOARD_SLOTS[kind])[:DASHBOARD_SLOTS[kind]]


class TestDashboard:
    def test_is_the_home_page_with_its_default_widgets(self, client):
        r = client.get("/")
        assert r.status_code == 200 and "<title>Dashboard · pledger</title>" in r.text
        assert slots(r.text, "cards") == default("cards") and slots(r.text, "plots") == default("plots")
        # The equity card, as in the Accounts tab, linking to it
        card = r.text.split('data-widget="equity"')[1].split("</article>")[0]
        assert '<a class="label" href="/accounts">Equity</a>' in card and "$1,060.75" in card
        assert 'id="rearrange" class="secondary compact" aria-pressed="false">Rearrange</button>' in r.text
        # A free slot opens its picker, which lists every widget of its kind
        assert r.text.count('class="widget-free"') == sum(default(k).count(None) for k in DASHBOARD_SLOTS)
        for kind, widgets in WIDGETS.items():
            picker = r.text.split(f'data-options="{kind}"')[1].split("</div>")[0]
            assert re.findall(r'name="widget" value="([\w-]+)"', picker) == list(widgets)
        # In groups: the accounts, then the results
        cards = r.text.split('data-options="cards"')[1].split("</div>")[0]
        assert [re.findall(r'value="([\w-]+)"', g) for g in cards.split("<legend>")[1:]] == [
            ["assets", "liabilities", "equity", "net-liquidity"],
            ["month-gains", "month-losses", "month-profit", "month-equity-change", "monthly-expenses", "months-covered"],
        ]
        plots = r.text.split('data-options="plots"')[1].split("</div>")[0]
        assert re.findall(r"<legend>(\w+)</legend>", plots) == ["Accounts", "Results"]
        assert [re.findall(r'value="([\w-]+)"', g) for g in plots.split("<legend>")[1:]] == [
            ["equity-over-time", "debt-ratio", "liquidity-ratio"],
            ["monthly-gains", "monthly-losses", "monthly-profit", "months-of-expenses"],
        ]

    @staticmethod
    def new_movement_links(client) -> list[str]:
        panel = client.get("/").text.split('class="new-movement-buttons"')[1].split("</nav>")[0]
        hrefs = re.findall(r'href="([^"]+)"', panel)
        # Each form is in its tab, with that id
        for href in hrefs:
            path, form = href.split("#")
            assert f'<form id="{form}"' in client.get(path).text, href
        return hrefs

    def test_new_movement_buttons_go_to_the_forms(self, client):
        hrefs = self.new_movement_links(client)
        assert hrefs == [
            "/currencies#conversion-form", "/work#collection-form", "/debt#debt-form",
            "/cart#cart-form", "/split#split-form", "/ledger#movement-form",
        ]
        # Only the forms their tabs show: no credit cards, services or investments here
        assert not {"/credit-cards#card-payment-form", "/services#service-payment-form", "/investments#invest-form"} & set(hrefs)

    def test_new_movement_button_of_the_card_payments(self, settings, tmp_path):
        path = tmp_path / "credit_cards.yaml"
        path.write_text(CREDIT_CARDS_YAML, encoding="utf-8")
        client = TestClient(create_app(replace(settings, credit_cards_path=path)))
        assert "/credit-cards#card-payment-form" in self.new_movement_links(client)

    def test_place_clear_and_move(self, client):
        r = client.post("/dashboard/place", data={"kind": "cards", "slot": 5, "widget": "assets"})
        assert r.status_code == 200 and "<html" not in r.text  # the slots alone
        assert slots(r.text, "cards")[5] == "assets"
        assert slots(client.get("/").text, "cards")[5] == "assets"  # kept
        # Moved to a free slot, then swapped with a used one
        r = client.post("/dashboard/move", data={"kind": "cards", "source": 5, "target": 7})
        assert slots(r.text, "cards")[5:] == [None, None, "assets"]
        r = client.post("/dashboard/move", data={"kind": "cards", "source": 7, "target": 0})
        assert slots(r.text, "cards")[0] == "assets" and slots(r.text, "cards")[7] == "equity"
        r = client.post("/dashboard/clear", data={"kind": "cards", "slot": 0})
        assert slots(r.text, "cards")[0] is None and "assets" not in slots(r.text, "cards")
        # The plots, on their own
        r = client.post("/dashboard/place", data={"kind": "plots", "slot": 2, "widget": "debt-ratio"})
        assert slots(r.text, "plots")[:3] == ["equity-over-time", "monthly-profit", "debt-ratio"]

    @pytest.mark.parametrize("data, error", [
        ({"kind": "cards", "slot": 0, "widget": "assets"}, "That slot is not free"),
        ({"kind": "cards", "slot": 5, "widget": "equity"}, "Equity is already in the dashboard"),
        ({"kind": "cards", "slot": 5, "widget": "equity-over-time"}, "No 'equity-over-time' to put in the cards"),
        ({"kind": "cards", "slot": 8, "widget": "assets"}, "No slot 9 of the cards"),
        ({"kind": "tables", "slot": 0, "widget": "assets"}, "No 'tables' slots"),
    ])
    def test_place_refuses(self, client, data, error):
        r = client.post("/dashboard/place", data=data)
        assert r.status_code == 400 and error in r.json()["detail"]

    def test_every_widget_renders(self, client):
        for kind, widgets in WIDGETS.items():
            for slot, widget in enumerate(list(widgets)[: DASHBOARD_SLOTS[kind]]):
                client.post("/dashboard/clear", data={"kind": kind, "slot": slot})
                r = client.post("/dashboard/place", data={"kind": kind, "slot": slot, "widget": widget})
                assert r.status_code == 200 and slots(r.text, kind)[slot] == widget, widget
        r = client.get("/")
        assert 'class="line-chart"' in r.text and 'class="line-chart columns"' in r.text

    def test_kept_in_its_yaml(self, settings, tmp_path):
        path = tmp_path / "dashboard.yaml"
        settings = replace(settings, dashboard_path=path)
        client = TestClient(create_app(settings))
        assert slots(client.get("/").text, "cards") == default("cards") and not path.exists()
        client.post("/dashboard/clear", data={"kind": "plots", "slot": 0})
        assert "plots:\n- null\n- monthly-profit\n" in path.read_text()
        assert slots(TestClient(create_app(settings)).get("/").text, "plots")[:2] == [None, "monthly-profit"]
        # Unknown widgets (and repeated ones) leave their slot free; a short list is padded
        path.write_text("cards: [gone, assets, assets]\n")
        r = TestClient(create_app(settings)).get("/")
        assert slots(r.text, "cards") == [None, "assets"] + [None] * 6
        assert slots(r.text, "plots") == [None] * DASHBOARD_SLOTS["plots"]

    def test_unreadable_yaml(self, settings, tmp_path):
        path = tmp_path / "dashboard.yaml"
        path.write_text("cards: [\n")
        r = TestClient(create_app(replace(settings, dashboard_path=path))).get("/")
        assert r.status_code == 500 and "Could not read the dashboard" in r.text

    def test_ledger_is_its_own_tab(self, client):
        r = client.get("/ledger")
        assert "<title>Ledger · pledger</title>" in r.text and 'data-sortable="movements"' in r.text
        assert 'hx-get="/ledger" hx-target="#view"' in r.text
        assert 'data-sortable="movements"' not in client.get("/").text
