"""services.py: the Services tab: the monthly values of the services and what is owed to them."""

from __future__ import annotations

from pledger.enums import CreditorKind
from pledger.models import DEBT_ACCOUNT, next_month
from pledger.web.store import (
    LINE_COLUMN,
    SERVICE_COLUMN,
    Snapshot,
    service_columns,
    service_months,
)
from pledger.web.views.common import movement_rows
from pledger.web.views.debt import debt_after


def build_services(snapshot: Snapshot, service: str = "") -> dict:
    """
    The services of the debt account, with their columns of the services CSV (see
    store._service_rows): each one's value this month, how it is owed (on a day of the
    month, or prorated weekly), what was owed to it this month, up to today, and in all.
    Each service also has what we owe it now. Also the rows of the services CSV, newest
    first, and the movements owed to the services, newest first (of `service` only, if
    any), each with what we owe that service right after it.
    """
    debt = next((a for a in snapshot.debt_accounts() if a.name == DEBT_ACCOUNT), None)
    services = debt.get_kind(CreditorKind.SERVICE) if debt else {}
    columns, months = service_columns(snapshot), service_months(snapshot)
    month = snapshot.today.strftime("%Y-%m")
    now = dict(months).get(month, {})
    rows = snapshot.service_rows
    charged = {c: 0.0 for c in columns}
    this_month = {c: 0.0 for c in columns}
    for column, date, amount in zip(rows[LINE_COLUMN], rows.move_date, rows.amount):
        value = float(amount.replace(",", ""))
        charged[column] += value
        if date.startswith(month):
            this_month[column] += value
    table = []
    for name, c in services.items():
        lines = [
            {
                "name": column, "base": spec["base"].name, "value": now.get(column, 0.0),
                "mode": f"On day {spec['day']}" if spec["day"] is not None else "Weekly",
                "this_month": this_month[column], "charged": charged[column],
            }
            for column, spec in columns.items() if spec["service"] == name
        ]
        table.append({
            "name": name, "columns": lines, "owed": c.amount,
            **{k: sum(line[k] for line in lines) for k in ("value", "this_month", "charged")},
        })
    names = [c for c in columns if any(c in values for _, values in months)]
    values = [
        {"month": m, "cells": [v.get(c) for c in names], "future": m > month}
        for m, v in sorted(months, reverse=True) if v
    ]
    if service not in services:
        service = ""
    picked = (rows[rows[SERVICE_COLUMN] == service] if service else rows).sort_values("move_date", kind="stable")
    generated = picked.drop(columns=[SERVICE_COLUMN, LINE_COLUMN])
    movements = movement_rows(snapshot, generated, snapshot.ledger(generated))
    after = debt_after(snapshot, [debt]) if debt else {}
    for m, (name, column) in zip(movements, list(zip(picked[SERVICE_COLUMN], picked[LINE_COLUMN]))[::-1]):
        m |= {"who": name, "column": column, "debt": after.get(m["index"])}
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
    month after the last one, empty, with the columns of the services CSV to fill in, by
    service (see store.service_columns), and the values of the last month to fill it in with.
    """
    by_service: dict[str, list[str]] = {}
    for column, spec in service_columns(snapshot).items():
        by_service.setdefault(spec["service"], []).append(column)
    months = dict(service_months(snapshot))
    last = max(months, default=None)
    if not month:
        month = next_month(last) if last else snapshot.today.strftime("%Y-%m")
    if values is None:
        values = {c: f"{v:,.2f}" for c, v in months.get(month, {}).items()}
    return {
        "month": month,
        "values": values,
        "columns_by_service": by_service,
        "editing": month in months,
        "autofill": (
            {"month": last, "fields": {f"column:{c}": f"{v:,.2f}" for c, v in months[last].items()}}
            if last else None
        ),
    }
