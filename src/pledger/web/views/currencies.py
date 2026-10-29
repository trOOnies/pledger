"""currencies.py: the Currencies tab: conversions, inflation and the values of the other currencies."""

from __future__ import annotations

import datetime as dt
import math

from pandas import Period, Series, Timedelta, Timestamp

from pledger.enums import AccountType, MovementType
from pledger.models import INTERMEDIARY, Currency, InvestmentAccount
from pledger.web.store import (
    BASE_AMOUNT,
    CONVERSION_BOUGHT,
    CONVERSION_SOLD,
    OTHER_AMOUNT,
    Snapshot,
    conversion_fingerprint,
    conversion_pairs,
    exchange_base_movement,
    is_cross,
)
from pledger.web.views.charts import (
    column_chart,
    multi_line_chart,
    real_values,
    sampled,
    stacked_chart,
)
from pledger.web.views.common import AVERAGE_MONTH_DAYS, month_end_balances, percent


def build_conversions(snapshot: Snapshot, currency: str = "", account: str = "") -> list[dict]:
    """
    The rows of the conversions CSV, newest first, with the amount in each currency,
    the value of the other currency in the base one they imply and the balance of the
    account in the other currency right after it, in that currency; with a `currency`,
    only those from or to it, and with an `account`, only those of that account. Each
    row of a cross conversion (see conversion_pairs) has the `pair` of the other one.
    """
    after = snapshot.conversion_balances()
    pairs = conversion_pairs(snapshot)
    conversions = []
    ids = [snapshot.ids[ix] for ix in snapshot.conversion_rows.index]  # in the order of the CSV
    for i, (ix, row) in enumerate(snapshot.conversions.iterrows()):
        base_amount = float(row[BASE_AMOUNT].replace(",", "") or 0)
        other_amount = float(row[OTHER_AMOUNT].replace(",", "") or 0)
        debit, credit = snapshot.accounts[row["acc_debit"]], snapshot.accounts[row["acc_credit"]]
        other = debit if not debit.currency.is_base_currency else credit
        conversions.append({
            "index": i,
            "id": ids[i],
            "fingerprint": conversion_fingerprint(row),
            "date": row["move_date"],
            "base": row["base_movement"],
            "debit": debit.name,
            "credit": credit.name,
            "amount": base_amount,
            "other_amount": other_amount,
            "currency": other.currency.name,
            "account": other.name,
            "other_balance": after.get(i),  # None if the currency has no values
            "bought": other is debit,  # the other currency comes in, else goes out
            "rate": base_amount / other_amount if other_amount else None,
            "comment": row.get("comment", ""),
            "pair": pairs.get(i),
            "pair_id": ids[pairs[i]] if i in pairs else None,
            "through": (credit if other is debit else debit).name,
        })
    conversions.sort(key=lambda c: c["date"])  # stable: in their order on each date
    conversions.reverse()
    return [
        c for c in conversions
        if (not currency or c["currency"] == currency) and (not account or c["account"] == account)
    ]


def conversion_filter(snapshot: Snapshot, currency: str = "", account: str = "") -> dict:
    """
    The conversions of the currency and the account picked in the filter (all without
    them), with the currencies to pick from and the accounts in another currency (of
    the currency picked, if any). An account not of the currency picked is dropped.
    """
    others = [c for c in snapshot.currencies if c != snapshot.base_currency.name]
    currency = currency if currency in others else ""
    accounts = [
        a.name for a in snapshot.accounts.values()
        if a.currency.name in others and a.acc_type != AccountType.EQUITY and (not currency or a.currency.name == currency)
    ]
    account = account if account in accounts else ""
    return {
        "conversions": build_conversions(snapshot, currency, account),
        "conversion_currencies": others,
        "conversion_currency": currency,
        "conversion_accounts": accounts,
        "conversion_account": account,
    }


def conversion_fields(snapshot: Snapshot, sold: str = "", bought: str = "", changed: str = "") -> dict:
    """
    The currencies sold and bought of a new conversion, and the asset accounts it can be
    from (in the sold currency) and to (in the bought one), or the fixed ones of its base
    movement (see exchange_base_movement). Between two other currencies, it is a cross
    one (see store.is_cross), through the INTERMEDIARY account. By default, the
    currencies of the last conversion, else
    selling the base currency for the first other one. When they clash (the same
    currency), the one not `changed` ("sold" or "bought") gives way.
    """
    names = list(snapshot.currencies)
    base = snapshot.base_currency.name
    others = [n for n in names if n != base]
    if sold not in names and bought not in names and len(snapshot.conversions):
        last = snapshot.conversions.iloc[-1]
        debit, credit = snapshot.accounts.get(last["acc_debit"]), snapshot.accounts.get(last["acc_credit"])
        if debit is not None and credit is not None:
            sold, bought = credit.currency.name, debit.currency.name
    if sold not in names:
        sold = base if bought != base else next(iter(others), base)
    if bought not in names:
        bought = next(iter(others), base) if sold == base else base
    if sold == bought and others:
        if changed == "bought":
            sold = base if bought != base else others[0]
        else:
            bought = base if sold != base else others[0]
    cross = is_cross(snapshot, sold, bought)
    try:
        if cross:
            missing = [n for n in (CONVERSION_SOLD, CONVERSION_BOUGHT) if n not in snapshot.conversion_base_movements()]
            if missing:
                raise ValueError(f"No {missing[0]!r} base movement: add it as an internal transfer of the base movements YAML")
            base_movement, missing = "", None
        else:
            base_movement, missing = exchange_base_movement(snapshot, sold, bought), None
    except ValueError as e:
        base_movement, missing = "", str(e)
    move_base = snapshot.base_movements.get(base_movement)

    def options(currency: str) -> list[str]:
        """The asset accounts in that currency, but the investment ones."""
        return [
            a.name for a in snapshot.accounts.values()
            if a.acc_type == AccountType.ASSET and a.currency.name == currency and not isinstance(a, InvestmentAccount)
        ]

    return {
        "currencies": names,
        "sold": sold,
        "bought": bought,
        "base_movement": base_movement,
        "missing": missing,
        "cross": cross,
        "through": INTERMEDIARY,
        "fields": {
            "from_fixed": move_base.acc_credit.name if move_base and move_base.acc_credit else None,
            "to_fixed": move_base.acc_debit.name if move_base and move_base.acc_debit else None,
            "from_options": options(sold),
            "to_options": options(bought),
        },
    }


def value_series(values: Series) -> list[dict]:
    """The {date, value} points of a series by date, with the change over the previous one (a ratio)."""
    series, prev = [], None
    for date, value in values.items():
        series.append({
            "date": date.date().isoformat(), "value": float(value),
            "change": value / prev - 1 if prev else None,
        })
        prev = value
    return series


def growth(values: Series) -> dict | None:
    """
    How much a series by date grew from its first value to its last one (a ratio),
    and the same growth as an average per month. None without two dates.
    """
    if len(values) < 2:
        return None
    total = float(values.iloc[-1] / values.iloc[0])
    days = (values.index[-1] - values.index[0]).days
    return {
        "first": values.index[0].date().isoformat(),
        "total": total - 1,
        "monthly": total ** (AVERAGE_MONTH_DAYS / days) - 1,
    }


def inflation_months(base: Currency, today: dt.date) -> list[dict]:
    """
    The inflation of each month of the base currency, with the CPI at its end and the
    inflation of the 12 months up to it (None before 12 months). A month that hasn't
    ended by `today` is `projected`: its inflation can't be known yet.
    """
    inflation, cpi = base.monthly_inflation, base.customer_price_index
    current = Period(today, freq="M")
    yearly = (1 + inflation).rolling(12).apply(lambda g: g.prod(), raw=True) - 1
    return [
        {
            "month": str(month),
            "value": float(value),
            "cpi": float(cpi[month.end_time.normalize()]),
            "last_12": None if math.isnan(yearly[month]) else float(yearly[month]),
            "projected": month >= current,
        }
        for month, value in inflation.items()
    ]


def cpi_chart(base: Currency, today: dt.date) -> dict | None:
    """
    The daily CPI of the base currency, sampled to draw: one line up to the end of
    the last month that has ended by `today`, and a dashed one, projected, after it.
    """
    cpi = base.customer_price_index
    boundary = Timestamp(today.replace(day=1)) - Timedelta(days=1)  # the end of the last month ended

    def line(name: str, values: Series, dashed: bool) -> dict:
        return {"name": name, "dashed": dashed, "series": [
            {"date": d.date().isoformat(), "value": float(v)} for d, v in sampled(values).items()
        ]}

    lines = [line("CPI", cpi[:boundary], False), line("Projected", cpi[boundary:], True)]
    return multi_line_chart([ln for ln in lines if ln["series"]])


def build_currencies(snapshot: Snapshot, today: dt.date | None = None) -> dict:
    """
    The currencies: the daily CPI of the base currency and its inflation in each month
    (projected for the months that haven't ended by `today`), and, for each other
    currency, its value in the base currency (nominal) and that value deflated by the
    CPI (real) over time; the balances of the accounts in each currency, and the share
    of equity in the other currencies (None without them or positive equity); and the
    share of each currency in the assets at each month end.
    """
    today = today or dt.date.today()
    base = snapshot.base_currency
    cpi = base.customer_price_index
    balances = snapshot.ledger().balances()
    # The results of revaluing each account in another currency, all time.
    results: dict[str, float] = {}
    for move in snapshot.ledger(snapshot.result_rows).movements:
        gain = move.move_type == MovementType.GAIN
        name = move.acc_debit.name if gain else move.acc_credit.name
        results[name] = results.get(name, 0.0) + (move.amount if gain else -move.amount)

    def totals(name: str) -> dict:
        accs = [
            a for a in snapshot.accounts.values()
            if a.currency.name == name and a.acc_type != AccountType.EQUITY
        ]
        own = [a for a in accs if a.name in snapshot.other_balances]
        return {
            "accounts": len(accs),
            # The accounts revalued in this currency (see store._result_rows), with
            # their balance in it and in the base currency.
            "own": [
                {
                    "name": a.name, "description": a.description, "type": a.acc_type.value,
                    "balance": snapshot.other_balances[a.name], "base_balance": balances.get(a.name, 0.0),
                    "result": results.get(a.name, 0.0),
                }
                for a in own
            ],
            "assets": sum(balances.get(a.name, 0.0) for a in accs if a.acc_type == AccountType.ASSET),
            "liabilities": sum(
                balances.get(a.name, 0.0) for a in accs if a.acc_type == AccountType.LIABILITY
            ),
        }

    # Each currency is charted in the series color of its order, the base one (the CPI) first.
    others = []
    for i, c in enumerate(snapshot.currencies.values()):
        if c.is_base_currency:
            continue
        values = c.base_currency_mapping
        real, real_date = real_values(values, cpi)
        series = [
            {**p, "real": None if math.isnan(r) else float(r)}
            for p, r in zip(value_series(values), real)
        ]
        others.append({
            "name": c.name,
            "description": c.description,
            "slot": i % 8 + 1,
            **totals(c.name),
            "last": series[-1] if series else None,
            "growth": growth(values),
            "real_growth": growth(real.dropna()),
            "real_date": real_date,
            "series": series,
            "chart": multi_line_chart([
                {"name": "Nominal", "series": series, "dashed": False},
                {"name": "Real", "series": [{"date": p["date"], "value": p["real"]} for p in series], "dashed": True},
            ]),
        })

    months = inflation_months(base, today)
    known = [m for m in months if not m["projected"]]
    monthly = month_end_balances(snapshot)
    assets = [a for a in snapshot.accounts.values() if a.acc_type == AccountType.ASSET]
    shares = {
        c: [a.name for a in assets if a.currency.name == c]
        for c in snapshot.currencies
        if any(a.currency.name == c for a in assets)
    }
    base_totals = totals(base.name)
    # The share of equity (assets minus liabilities) in the accounts in other currencies, today
    other_equity = sum(o["assets"] - o["liabilities"] for o in others)
    equity = base_totals["assets"] - base_totals["liabilities"] + other_equity
    return {
        "base": {"name": base.name, "description": base.description, **base_totals},
        "others": others,
        "other_equity": other_equity,
        "other_equity_share": other_equity / equity if equity > 1e-9 and others else None,
        "last_month": known[-1] if known else None,  # the last month ended
        "last_12_monthly": (
            (1 + known[-1]["last_12"]) ** (1 / 12) - 1 if known and known[-1]["last_12"] is not None else None
        ),
        "cpi_chart": cpi_chart(base, today),
        "inflation_months": months,
        "inflation_chart": column_chart(months, percent) if months else None,
        "currency_shares": stacked_chart(monthly, shares),
    }
