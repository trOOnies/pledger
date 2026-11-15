"""dashboard.py: the Dashboard tab: number cards and plots in slots, and the New movement buttons."""

from __future__ import annotations

import datetime as dt

from pledger.enums import AccountType, CreditorKind, DebtRole, MovementType
from pledger.models import CreditCardAccount, InvestmentAccount, next_month
from pledger.web.store import DASHBOARD_SLOTS, Dashboard, Snapshot, today
from pledger.web.views.accounts import over_time_charts
from pledger.web.views.charts import column_chart, line_chart
from pledger.web.views.debt import kind_movements, net_creditors
from pledger.web.views.results import equity_metrics, equity_series, expense_months

PLOT_MONTHS = 12  # the monthly plots show this many months, up to this one

# What can go in each kind of slot, in the order of its picker: its name to its title, what it
# shows, the tab it comes from and its group in the picker
WIDGETS = {
    "cards": {
        "assets": ("Assets", "Total balance of the asset accounts, today", "/accounts", "Accounts"),
        "liabilities": ("Liabilities", "Total balance of the liability accounts, today", "/accounts", "Accounts"),
        "equity": ("Equity", "Assets minus liabilities, today", "/accounts", "Accounts"),
        "net-liquidity": ("Net liquidity", "Liquid assets minus liquid liabilities, today", "/accounts", "Accounts"),
        "month-gains": ("Gains this month", "The gains of this month, without the initial movements", "/results", "Results"),
        "month-losses": ("Losses this month", "The losses of this month, without the initial movements", "/results", "Results"),
        "month-profit": ("Profit this month", "Gains minus losses of this month, without the initial movements", "/results", "Results"),
        "month-equity-change": ("Equity change this month", "How much equity changed in this month, nominal", "/results", "Results"),
        "monthly-expenses": ("Monthly expenses", "What you spent on average each month of the last year, without the initial, internal and Employment losses", "/results", "Results"),
        "months-covered": ("Months of expenses", "How many months your equity would last, spending your monthly expenses", "/results", "Results"),
    },
    "plots": {
        "equity-over-time": ("Equity over time", "Equity at each entry date, all time", "/results", "Accounts"),
        "debt-ratio": ("Debt over equity", "Liabilities over equity, at each month end", "/accounts", "Accounts"),
        "liquidity-ratio": ("Net liquidity over equity", "Liquid assets minus liquid liabilities over equity, at each month end", "/accounts", "Accounts"),
        "monthly-gains": ("Gains by month", f"The gains of each of the last {PLOT_MONTHS} months", "/results", "Results"),
        "monthly-losses": ("Losses by month", f"The losses of each of the last {PLOT_MONTHS} months", "/results", "Results"),
        "monthly-profit": ("Profit by month", f"Gains minus losses of each of the last {PLOT_MONTHS} months", "/results", "Results"),
        "months-of-expenses": ("Equity in months of expenses", "How many months your equity would last at each month end, like the Months of expenses card", "/results", "Results"),
    },
}


def dashboard_options(kind: str) -> list[dict]:
    """The widgets of a kind of slot, in order, by group, for its picker."""
    groups: dict[str, list[dict]] = {}
    for key, (title, description, _, group) in WIDGETS[kind].items():
        groups.setdefault(group, []).append({"key": key, "title": title, "description": description})
    return [{"name": name, "options": options} for name, options in groups.items()]


def valid_dashboard(dashboard: Dashboard) -> Dashboard:
    """The dashboard with the slots of unknown widgets (e.g. of an older pledger) freed, and each widget only once."""
    valid = {}
    for kind, names in dashboard.items():
        seen, slots = set(), []
        for name in names:
            ok = name in WIDGETS[kind] and name not in seen
            seen.add(name)
            slots.append(name if ok else None)
        valid[kind] = slots
    return valid


def _month_totals(snapshot: Snapshot) -> dict[str, dict[MovementType, float]]:
    """The gains and losses of each month, without the initial movements."""
    rows = snapshot.all_rows
    rows = rows[~rows.base_movement.isin(snapshot.initial_base_names)]
    totals: dict[str, dict[MovementType, float]] = {}
    for move in snapshot.ledger(rows).movements:
        if move.move_type in (MovementType.GAIN, MovementType.LOSS):
            month = totals.setdefault(str(move.move_date)[:7], {MovementType.GAIN: 0.0, MovementType.LOSS: 0.0})
            month[move.move_type] += move.amount
    return totals


def _last_months(month: str, count: int) -> list[str]:
    """The `count` months up to `month`, oldest first."""
    first = dt.date.fromisoformat(f"{month}-01")
    for _ in range(count - 1):
        first = (first - dt.timedelta(days=1)).replace(day=1)
    months = [first.isoformat()[:7]]
    while len(months) < count:
        months.append(next_month(months[-1]))
    return months


class _Widgets:
    """Builds the widgets placed in the dashboard, sharing what several of them need."""

    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot
        self.month = today().isoformat()[:7]
        self.month_label = dt.date.fromisoformat(f"{self.month}-01").strftime("%b %Y")
        self._cache: dict[str, object] = {}

    def _once(self, key: str, build):
        if key not in self._cache:
            self._cache[key] = build()
        return self._cache[key]

    def balances(self) -> dict[str, float]:
        def build():
            balances = self.snapshot.ledger().balances()
            accounts = self.snapshot.accounts.values()

            def total(acc_type: AccountType, liquid: bool | None = None) -> float:
                return sum(
                    balances.get(a.name, 0.0) for a in accounts
                    if a.acc_type == acc_type and liquid in (None, a.is_liquid)
                )

            assets, liabilities = total(AccountType.ASSET), total(AccountType.LIABILITY)
            return {
                "assets": assets, "liabilities": liabilities, "equity": assets - liabilities,
                "net-liquidity": total(AccountType.ASSET, True) - total(AccountType.LIABILITY, True),
            }
        return self._once("balances", build)

    def month_totals(self):
        return self._once("month_totals", lambda: _month_totals(self.snapshot))

    def metrics(self, month: str = "") -> dict:
        return self._once(f"metrics {month}", lambda: equity_metrics(self.snapshot, month))

    def card(self, key: str) -> dict:
        """A number card: its value (None if there is none yet), how to show it and a note."""
        value, unit, note, tone = None, "money", "today", None
        if key in ("equity", "assets", "liabilities", "net-liquidity"):
            value = self.balances()[key]
            tone = "loss" if key != "liabilities" and value < -0.005 else None
        elif key.startswith("month-") and key != "month-equity-change":
            totals = self.month_totals().get(self.month, {MovementType.GAIN: 0.0, MovementType.LOSS: 0.0})
            gains, losses = totals[MovementType.GAIN], totals[MovementType.LOSS]
            value = {"month-profit": gains - losses, "month-gains": gains, "month-losses": losses}[key]
            note = f"in {self.month_label}"
            if key == "month-profit":
                tone = "gain" if value > 0.005 else "loss" if value < -0.005 else None
        elif key == "month-equity-change":
            value, unit, note = self.metrics(self.month)["equity_change"], "percent", f"in {self.month_label}"
            if value is not None:
                tone = "gain" if value > 0 else "loss" if value < 0 else None
        elif key == "monthly-expenses":
            value, note = self.metrics()["monthly_expenses"], "a month, in the last year"
        elif key == "months-covered":
            value, unit, note = self.metrics()["months_covered"], "months", "that your equity would last"
            tone = "loss" if value is not None and value < 0 else None
        return {"value": value, "unit": unit, "note": note, "tone": tone}

    def monthly_chart(self, key: str) -> dict:
        totals = self.month_totals()
        series = []
        for month in _last_months(self.month, PLOT_MONTHS):
            t = totals.get(month, {MovementType.GAIN: 0.0, MovementType.LOSS: 0.0})
            gains, losses = t[MovementType.GAIN], t[MovementType.LOSS]
            value = {"monthly-profit": gains - losses, "monthly-gains": gains, "monthly-losses": losses}[key]
            series.append({"month": month, "value": value})
        return {"chart": column_chart(series), "series": series}

    def plot(self, key: str) -> dict:
        """A plot: its chart (None without data) and the series behind it."""
        if key == "equity-over-time":
            series = equity_series(self.snapshot)
            return {"chart": line_chart(series), "series": series}
        if key in ("monthly-gains", "monthly-losses", "monthly-profit"):
            return self.monthly_chart(key)
        if key == "months-of-expenses":
            expenses = expense_months(self.snapshot)
            return {"chart": expenses["chart"], "series": expenses["months"]}
        charts = self._once("over_time", lambda: over_time_charts(self.snapshot))
        name = key.replace("-", "_")
        return {"chart": charts[name], "series": charts[name.replace("_ratio", "_series")]}


def build_dashboard(snapshot: Snapshot, dashboard: Dashboard) -> dict:
    """What the slots of the dashboard show: None for a free slot, else its widget, built."""
    widgets = _Widgets(snapshot)
    slots = {}
    for kind, names in valid_dashboard(dashboard).items():
        build = widgets.card if kind == "cards" else widgets.plot
        slots[kind] = [
            None if name is None else {
                "key": name, "title": WIDGETS[kind][name][0], "description": WIDGETS[kind][name][1],
                "href": WIDGETS[kind][name][2], **build(name),
            }
            for name in names
        ]
    return {"slots": slots, "slot_counts": DASHBOARD_SLOTS, "options": {k: dashboard_options(k) for k in WIDGETS}}


def new_movement_buttons(snapshot: Snapshot) -> list[dict]:
    """
    The buttons of the New movement panel: each goes to the form of a tab that adds movements,
    if that tab shows it (e.g. the conversions need a second currency).
    """
    accounts = snapshot.accounts.values()
    people = any(a.kind == CreditorKind.PERSON for a in snapshot.debt_accounts())
    service_accounts, _ = kind_movements(snapshot, CreditorKind.SERVICE)
    services = bool(service_accounts) and bool(net_creditors(snapshot, service_accounts)[0])
    invest = any(isinstance(a, InvestmentAccount) for a in accounts) and bool(snapshot.invest_base_movements())
    company = snapshot.base_debt_account(DebtRole.DEBTOR, CreditorKind.COMPANY) is not None
    buttons = [
        ("💱", "Conversion", "Currencies", "/currencies#conversion-form", len(snapshot.currencies) > 1),
        ("💼", "Salary received", "Work", "/work#collection-form", company),
        ("📈", "Investment movement", "Investments", "/investments#invest-form", invest),
        ("🤝", "Debt movement", "People", "/debt#debt-form", people),
        ("🧾", "Service payment", "Services", "/services#service-payment-form", services),
        ("💳", "Card payment", "Credit Cards", "/credit-cards#card-payment-form", any(isinstance(a, CreditCardAccount) for a in accounts)),
        ("🛒", "Cart", "Cart", "/cart#cart-form", True),
        ("🍕", "Split", "Split", "/split#split-form", True),
        ("📋", "Movement", "Ledger", "/ledger#movement-form", True),
    ]
    return [
        {"emoji": emoji, "label": label, "tab": tab, "href": href}
        for emoji, label, tab, href, shown in buttons if shown
    ]
