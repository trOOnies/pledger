"""services.py: the monthly values of the services of the debt account, and what is owed to them."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from pandas import DataFrame

from pledger.enums import CreditorKind, LedgerSide, MovementType
from pledger.models import DEBT_ACCOUNT, DebtAccount, next_month, parse_month
from pledger.web.store.common import _generated_rows, _parse_amount
from pledger.web.store.constants import (
    ID_COLUMN,
    LINE_COLUMN,
    SERVICE_COLUMN,
    SERVICE_ID,
    SERVICE_MONTH,
)
from pledger.weeks import prorate_by_week

if TYPE_CHECKING:
    from pledger.web.store.snapshot import Snapshot


def service_columns(snapshot: Snapshot) -> dict[str, dict]:
    """
    The columns of the services of the debt account (see Creditor.columns), each with its
    `service`, its `base` movement, a loss of one's own (not internal) that may credit
    the debt account, and its `day` (None if prorated weekly).
    """
    debt = snapshot.accounts.get(DEBT_ACCOUNT)
    services = debt.get_kind(CreditorKind.SERVICE) if isinstance(debt, DebtAccount) else {}
    columns = {}
    for service in services.values():
        for column, spec in service.columns.items():
            base = snapshot.base_movements.get(spec["base_movement"])
            if (
                base is None or base.internal or base.move_type != MovementType.LOSS
                or base.side(LedgerSide.CREDIT) not in {None, DEBT_ACCOUNT}
            ):
                raise ValueError(
                    f"The base movement of a column of a service must be a loss, not internal, that may "
                    f"credit {DEBT_ACCOUNT}: not {spec['base_movement']!r} ({service.name}, {column})"
                )
            columns[column] = {"service": service.name, "base": base, "day": spec["day"]}
    return columns


def service_months(snapshot: Snapshot) -> list[tuple[str, dict[str, float]]]:
    """
    The rows of the services CSV: each month, once, with the value of each column with
    one (empty ones are none). Every column is one of a service (see service_columns).
    """
    columns = service_columns(snapshot)
    unknown = [c for c in snapshot.services.columns if c != SERVICE_MONTH and c not in columns]
    if unknown:
        raise ValueError(
            f"The services CSV columns {unknown} aren't columns of any service of {DEBT_ACCOUNT}: add them "
            "under the `columns` of their service in the creditors YAML"
        )
    found, seen = [], set()
    for row in snapshot.services.to_dict("records"):
        month = parse_month(row[SERVICE_MONTH], "month of the services CSV")
        if month in seen:
            raise ValueError(f"The services CSV has more than one row of {month}")
        seen.add(month)
        values = {
            c: _parse_amount(text, f"value of {c} in {month} of the services CSV")
            for c, text in row.items() if c != SERVICE_MONTH and str(text).strip()
        }
        found.append((month, values))
    return found


def _service_rows(snapshot: Snapshot, start: int) -> DataFrame:
    """
    What is owed to the services, as CSV rows indexed from `start` down, with their
    SERVICE_COLUMN and LINE_COLUMN: each value of the services CSV, up to today, as a
    movement of its column's base movement crediting the debt account, its comment
    naming the service first (see DebtAccount.creditor_in). With a day, it is owed whole
    on that day of the month (or its last one); without one, split between the weeks of
    the month, on their Mondays (see weeks.prorate_by_week), like the salaries. Never
    before the first date of the ledger or in the previous month.
    """
    columns, new_rows, now = service_columns(snapshot), [], snapshot.today

    def add(date: dt.date, column: str, value: float, move_id: str, when: str) -> None:
        day = date.isoformat()
        if value == 0 or date > now or day < snapshot.start_date:
            return
        spec = columns[column]
        base, service = spec["base"], spec["service"]
        new_rows.append({
            "move_date": day, "base_movement": base.name, "acc_debit": base.acc_debit.name,
            "acc_credit": DEBT_ACCOUNT, "amount": f"{value:,.2f}",
            "comment": f"{service}: {'' if column == service else column + ', '}{when}",
            ID_COLUMN: f"{SERVICE_ID}-{column}-{move_id}", SERVICE_COLUMN: service, LINE_COLUMN: column,
        })

    for month, values in sorted(service_months(snapshot)):
        if snapshot.previous_month and month <= snapshot.previous_month:
            continue
        for column, amount in values.items():
            if columns[column]["day"] is None:
                weeks = prorate_by_week(month, amount)
                for n, (week, value) in enumerate(weeks, 1):
                    add(week.start, column, value, f"{month}-{n}", f"week {n}/{len(weeks)} of {month}")
            else:
                first = dt.date.fromisoformat(f"{month}-01")
                days = (dt.date.fromisoformat(f"{next_month(month)}-01") - first).days
                add(first.replace(day=min(columns[column]["day"], days)), column, amount, month, month)
    return _generated_rows(new_rows, [*snapshot.rows.columns, SERVICE_COLUMN, LINE_COLUMN], start)
