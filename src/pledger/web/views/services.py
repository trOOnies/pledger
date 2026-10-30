"""services.py: the Services tab: the monthly values of the services and what is owed to them."""

from __future__ import annotations

from pledger.enums import CreditorKind
from pledger.models import DEBT_ACCOUNT, next_month
from pledger.web.store import (
    SERVICE_COLUMN,
    Snapshot,
    service_bases,
    service_months,
)
from pledger.web.views.common import movement_rows
from pledger.web.views.debt import debt_after


def build_services(snapshot: Snapshot, service: str = "") -> dict:
    """
    The services of the debt account (see store._service_rows): each one's base movement
    and value this month, what was owed to it this month, up to today, and in all, and
    what we owe it now. Also the rows of the services CSV, newest first, and the movements
    owed to the services, newest first (of `service` only, if any), each with what we owe
    that service right after it.
    """
    debt = next((a for a in snapshot.debt_accounts() if a.name == DEBT_ACCOUNT), None)
    services = debt.get_kind(CreditorKind.SERVICE) if debt else {}
    bases, months = service_bases(snapshot), service_months(snapshot)
    month = snapshot.today.strftime("%Y-%m")
    now = dict(months).get(month, {})
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
            "name": name, "base": bases[name].name if name in bases else None, "owed": c.amount,
            "value": now.get(name, 0.0), "this_month": this_month[name], "charged": charged[name],
        }
        for name, c in services.items()
    ]
    names = [c for c in bases if any(c in values for _, values in months)]
    values = [
        {"month": m, "cells": [v.get(c) for c in names], "future": m > month}
        for m, v in sorted(months, reverse=True) if v
    ]
    if service not in services:
        service = ""
    picked = (rows[rows[SERVICE_COLUMN] == service] if service else rows).sort_values("move_date", kind="stable")
    generated = picked.drop(columns=[SERVICE_COLUMN])
    movements = movement_rows(snapshot, generated, snapshot.ledger(generated))
    after = debt_after(snapshot, [debt]) if debt else {}
    for m, name in zip(movements, list(picked[SERVICE_COLUMN])[::-1]):
        m |= {"who": name, "debt": after.get(m["index"])}
    return {
        "has_debt": debt is not None,
        "services": table,
        "columns": names,
        "values": values,
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
    months = dict(service_months(snapshot))
    last = max(months, default=None)
    if not month:
        month = next_month(last) if last else snapshot.today.strftime("%Y-%m")
    if values is None:
        values = {c: f"{v:,.2f}" for c, v in months.get(month, {}).items()}
    return {
        "month": month,
        "values": values,
        "services": list(service_bases(snapshot)),
        "editing": month in months,
        "autofill": (
            {"month": last, "fields": {f"service:{c}": f"{v:,.2f}" for c, v in months[last].items()}}
            if last else None
        ),
    }
