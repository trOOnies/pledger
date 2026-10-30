"""services.py: the monthly values of the services of the debt account, and what is owed to them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pandas import DataFrame

from pledger.enums import CreditorKind, LedgerSide, MovementType
from pledger.models import DEBT_ACCOUNT, DebtAccount, parse_month
from pledger.web.store.common import _generated_rows, _parse_amount
from pledger.web.store.constants import (
    ID_COLUMN,
    SERVICE_COLUMN,
    SERVICE_ID,
    SERVICE_MONTH,
)
from pledger.weeks import prorate_by_week

if TYPE_CHECKING:
    from pledger.models import MovementBase
    from pledger.web.store.snapshot import Snapshot


def service_bases(snapshot: Snapshot) -> dict[str, MovementBase]:
    """
    The services of the debt account with a base movement (see Creditor.base_movement), a
    loss of one's own (not internal) that may credit the debt account: their columns of
    the services CSV.
    """
    debt = snapshot.accounts.get(DEBT_ACCOUNT)
    services = debt.get_kind(CreditorKind.SERVICE) if isinstance(debt, DebtAccount) else {}
    bases = {}
    for service in services.values():
        if service.base_movement is None:
            continue
        base = snapshot.base_movements.get(service.base_movement)
        if (
            base is None or base.internal or base.move_type != MovementType.LOSS
            or base.side(LedgerSide.CREDIT) not in {None, DEBT_ACCOUNT}
        ):
            raise ValueError(
                f"The base movement of a service must be a loss, not internal, that may "
                f"credit {DEBT_ACCOUNT}: not {service.base_movement!r} ({service.name})"
            )
        bases[service.name] = base
    return bases


def service_months(snapshot: Snapshot) -> list[tuple[str, dict[str, float]]]:
    """
    The rows of the services CSV: each month, once, with the value of each column with
    one (empty ones are none). Every column is a service (see service_bases).
    """
    bases = service_bases(snapshot)
    unknown = [c for c in snapshot.services.columns if c != SERVICE_MONTH and c not in bases]
    if unknown:
        raise ValueError(
            f"The services CSV columns {unknown} aren't services of {DEBT_ACCOUNT} with a base movement: "
            "add them to the creditors YAML with their `base_movement`"
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
    SERVICE_COLUMN: each value of the services CSV, up to today, split between the weeks
    of its month, on their Mondays (see weeks.prorate_by_week), like the salaries, as
    movements of its service's base movement crediting the debt account, their comment
    naming the service first (see DebtAccount.creditor_in). Never before the first date
    of the ledger or in the previous month.
    """
    bases, new_rows, now = service_bases(snapshot), [], snapshot.today
    for month, values in sorted(service_months(snapshot)):
        if snapshot.previous_month and month <= snapshot.previous_month:
            continue
        for service, amount in values.items():
            weeks = prorate_by_week(month, amount)
            for n, (week, value) in enumerate(weeks, 1):
                day = week.start.isoformat()
                if value == 0 or week.start > now or day < snapshot.start_date:
                    continue
                new_rows.append({
                    "move_date": day, "base_movement": bases[service].name,
                    "acc_debit": bases[service].acc_debit.name, "acc_credit": DEBT_ACCOUNT,
                    "amount": f"{value:,.2f}", "comment": f"{service}: week {n}/{len(weeks)} of {month}",
                    ID_COLUMN: f"{SERVICE_ID}-{service}-{month}-{n}", SERVICE_COLUMN: service,
                })
    return _generated_rows(new_rows, [*snapshot.rows.columns, SERVICE_COLUMN], start)
