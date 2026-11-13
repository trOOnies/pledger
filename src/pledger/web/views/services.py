"""services.py: the Services tab: the monthly values of the services and what is owed to them."""

from __future__ import annotations

from pledger.enums import CreditorKind, DebtRole, LedgerSide
from pledger.models import DebtAccount, next_month
from pledger.web.store import (
    SERVICE_COLUMN,
    Snapshot,
    service_account,
    service_bases,
    service_cells,
    service_estimates,
    service_months,
    service_payment_base,
)
from pledger.web.views.common import movement_rows
from pledger.web.views.debt import debt_after, net_creditors
from pledger.web.views.forms import by_group


def build_services(snapshot: Snapshot, service: str = "") -> dict:
    """
    The services of the debt accounts (see store._service_rows): each one's debt account
    (see store.service_account), base movement and value this month, what was owed to it
    this month, up to today, and in all, and what we owe it now, in both debt accounts. Also the rows of the services CSV, newest first, and the movements
    owed to the services, newest first (of `service` only, if any), each with what we owe
    that service right after it. The estimated values are marked, and `overdue` has the
    months already ended with any (see store.service_estimates).
    """
    accounts = snapshot.debt_accounts()
    services = {n: c for n, c in net_creditors(snapshot, accounts)[0].items() if c.kind == CreditorKind.SERVICE}
    bases, months = service_bases(snapshot), service_months(snapshot)
    month = snapshot.today.strftime("%Y-%m")
    now = dict(months).get(month, {})
    estimates = service_estimates(snapshot)
    rows = snapshot.service_rows
    charged = dict.fromkeys(services, 0.0)
    this_month = dict.fromkeys(services, 0.0)
    for name, date, amount in zip(rows[SERVICE_COLUMN], rows.move_date, rows.amount):
        value = float(amount.replace(",", ""))
        charged[name] += value
        if date.startswith(month):
            this_month[name] += value
    table = [
        {
            "name": name, "account": service_account(snapshot, c), "base": bases[name].name if name in bases else None,
            "owed": c.amount,
            "value": now.get(name, 0.0), "estimated": name in estimates.get(month, []),
            "this_month": this_month[name], "charged": charged[name],
        }
        for name, c in services.items()
    ]
    names = [c for c in bases if any(c in values for _, values in months)]
    values = [
        {
            "month": m, "cells": [v.get(c) for c in names], "future": m > month,
            "estimated": [c in estimates.get(m, []) for c in names],
        }
        for m, v in sorted(months, reverse=True) if v
    ]
    if service not in services:
        service = ""
    picked = (rows[rows[SERVICE_COLUMN] == service] if service else rows).sort_values("move_date", kind="stable")
    generated = picked.drop(columns=[SERVICE_COLUMN])
    movements = movement_rows(snapshot, generated, snapshot.ledger(generated))
    after = debt_after(snapshot, accounts)
    for m, name in zip(movements, list(picked[SERVICE_COLUMN])[::-1]):
        m |= {"who": name, **after.get(m["index"], {"debt": None})}
    return {
        "has_debt": bool(accounts),
        # The accounts the services are owed in: those of the base currency (see store.service_account)
        "debt_name": (de.name if (de := snapshot.base_debt_account(DebtRole.CREDITOR)) else ""),
        "loan_name": (lo.name if (lo := snapshot.base_debt_account(DebtRole.DEBTOR)) else ""),
        "services": table,
        "columns": names,
        "values": values,
        "overdue": [{"month": m, "services": estimates[m]} for m in sorted(estimates) if m < month],
        "movements": movements,
        "service": service,
        "month": month,
        "monthly": sum(s["value"] for s in table),
        "this_month": sum(s["this_month"] for s in table),
        "owed": sum(s["owed"] for s in table),
    }


def service_form(snapshot: Snapshot, month: str = "", values: dict | None = None) -> dict:
    """
    The state of the services month form: a month's values to edit it, or by default the
    month after the last one, empty, with the services to fill in (see store.service_bases),
    and the values of the last month to fill it in with.
    """
    months = service_cells(snapshot)
    last = max(months, default=None)
    if not month:
        month = next_month(last) if last else snapshot.today.strftime("%Y-%m")
    if values is None:
        values = months.get(month, {})
    return {
        "month": month,
        "values": values,
        "services": list(service_bases(snapshot)),
        "editing": month in months,
        "autofill": (
            {"month": last, "fields": {f"service:{c}": v for c, v in months[last].items()}}
            if last else None
        ),
    }


def service_payment_form(snapshot: Snapshot, service: str = "", month: str = "", values: dict | None = None) -> dict:
    """
    The state of the service payment form: the service (the first one by default) and
    month (this one by default) paid, its value then if known, to fill the amount in, else
    its estimate, if any, and the accounts in the base currency it can be paid from, on the
    credit of its base movement (see store.service_payment_base), but the debt accounts. Only the months whose
    value isn't known can take the amount paid as it.
    """
    bases = service_bases(snapshot)
    if service not in bases:
        service = next(iter(bases), "")
    month = month or snapshot.today.strftime("%Y-%m")
    values = dict(values or {})
    value = dict(service_months(snapshot)).get(month, {}).get(service)
    estimated = service in service_estimates(snapshot).get(month, [])
    known = value is not None and not estimated
    base, debt, options = "", "", []
    if service:
        base, debt = service_payment_base(snapshot, service)
        movement = snapshot.base_movements.get(base)
        if movement is not None:
            options = [
                a.name for a in snapshot.side_accounts(movement, LedgerSide.CREDIT)
                if a.currency.is_base_currency and not isinstance(a, DebtAccount)
            ]
    if "amount" not in values:
        values["amount"] = f"{value:,.2f}" if known else ""
    return {
        "services": list(bases),
        "service": service,
        "month": month,
        "known": value if known else None,
        "estimate": value if estimated else None,
        "payment_base": base,  # missing from the base movements YAML, if not in base_movements
        "base_movement": base if base in snapshot.base_movements else "",
        "debt_account": debt,
        "groups": by_group(snapshot, options),
        "values": values,
    }
