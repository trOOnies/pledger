"""services.py: the monthly values of the services of the debt accounts, and what is owed to them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pandas import DataFrame, Period, Timestamp

from pledger.enums import CreditorKind, DebtRole, LedgerSide, MovementType
from pledger.models import (
    ESTIMATED,
    Creditor,
    DebtAccount,
    parse_month,
    price_growth,
)
from pledger.web.store.common import _generated_rows, _parse_amount
from pledger.web.store.constants import (
    DEBT_PAID,
    ID_COLUMN,
    LOAN_GIVEN,
    SERVICE_COLUMN,
    SERVICE_ID,
    SERVICE_MONTH,
)
from pledger.weeks import prorate_by_week

if TYPE_CHECKING:
    from pledger.models import MovementBase
    from pledger.web.store.snapshot import Snapshot


def services_of(snapshot: Snapshot) -> dict[str, Creditor]:
    """The services of the debt accounts of the services (SeD and SeL), each once (they share the creditors YAML)."""
    services = {}
    for a in snapshot.accounts.values():
        if isinstance(a, DebtAccount):
            services |= {n: c for n, c in a.get_kind(CreditorKind.SERVICE).items() if n not in services}
    return services


def service_account(snapshot: Snapshot, service: Creditor) -> str:
    """
    The debt account of the services a service is owed its values of the services CSV in:
    that of the first role it can be in (see Creditor.roles). SeD unless it is expected to be
    a debtor (it charges in advance), then SeL.
    """
    account = snapshot.base_debt_account(service.roles[0], CreditorKind.SERVICE)
    if account is None:
        raise ValueError(f"No {service.roles[0].value} account of the services for the service {service.name!r}")
    return account.name


def service_payment_base(snapshot: Snapshot, service: str) -> tuple[str, str]:
    """
    The base movement paying a service (see service_account), and its debt account, the
    debit of it: Debt - Paid in SeD, else Loan - Given in SeL, for the ones expected to be
    debtors (they charge in advance).
    """
    account = service_account(snapshot, services_of(snapshot)[service])
    role = snapshot.accounts[account].role
    return (DEBT_PAID if role == DebtRole.CREDITOR else LOAN_GIVEN), account


def service_bases(snapshot: Snapshot) -> dict[str, MovementBase]:
    """
    The services of the debt accounts with a base movement (see Creditor.base_movement), a
    loss of one's own (not internal) that may credit its debt account (see
    service_account): their columns of the services CSV.
    """
    bases = {}
    for service in services_of(snapshot).values():
        if service.base_movement is None:
            continue
        base = snapshot.base_movements.get(service.base_movement)
        account = service_account(snapshot, service)
        if (
            base is None or base.internal or base.move_type != MovementType.LOSS
            or base.side(LedgerSide.CREDIT) not in {None, account}
        ):
            raise ValueError(
                f"The base movement of a service must be a loss, not internal, that may "
                f"credit its debt account, {account}: not {service.base_movement!r} ({service.name})"
            )
        bases[service.name] = base
    return bases


def service_months(snapshot: Snapshot) -> list[tuple[str, dict[str, float]]]:
    """
    The rows of the services CSV: each month, once, with the value of each column with
    one (empty ones are none). Every column is a service (see service_bases). An
    ESTIMATED cell (see service_estimates) has the last known value of its service, of
    an earlier month, grown by the inflation of the months since then, to the cent.
    """
    bases = service_bases(snapshot)
    unknown = [c for c in snapshot.services.columns if c != SERVICE_MONTH and c not in bases]
    if unknown:
        raise ValueError(
            f"The services CSV columns {unknown} aren't services of the debt accounts with a base movement: "
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
            for c, text in row.items()
            if c != SERVICE_MONTH and str(text).strip() and not _is_estimated(text)
        }
        found.append((month, values))
    estimates, months = service_estimates(snapshot), dict(found)
    inflation = snapshot.base_currency.monthly_inflation
    last: dict[str, tuple[str, float]] = {}  # the last known value of each service, with its month
    for month in sorted(months):
        last |= {c: (month, v) for c, v in months[month].items()}
        for service in estimates.get(month, []):
            if service not in last:
                raise ValueError(
                    f"The services CSV has {service} {ESTIMATED} in {month}, with no known value before it"
                )
            known, value = last[service]
            months[month][service] = round(value * price_growth(inflation, _month_end(known), _month_end(month)), 2)
    return found


def service_estimates(snapshot: Snapshot) -> dict[str, list[str]]:
    """The services with an ESTIMATED cell in each month of the services CSV that has any."""
    found = {}
    for row in snapshot.services.to_dict("records"):
        services = [c for c, text in row.items() if c != SERVICE_MONTH and _is_estimated(text)]
        if services:
            found[parse_month(row[SERVICE_MONTH], "month of the services CSV")] = services
    return found


def service_cells(snapshot: Snapshot) -> dict[str, dict[str, str]]:
    """Each month's values of the services CSV as written in a form: ESTIMATED ones as such."""
    estimates = service_estimates(snapshot)
    return {
        month: {c: ESTIMATED if c in estimates.get(month, []) else f"{v:,.2f}" for c, v in values.items()}
        for month, values in service_months(snapshot)
    }


def _is_estimated(text) -> bool:
    return str(text).strip() == ESTIMATED


def _month_end(month: str) -> Timestamp:
    return Period(month, freq="M").end_time.normalize()


def _service_rows(snapshot: Snapshot, start: int) -> DataFrame:
    """
    What is owed to the services, as CSV rows indexed from `start` down, with their
    SERVICE_COLUMN: each value of the services CSV, up to today, split between the weeks
    of its month, on their Mondays (see weeks.prorate_by_week), like the salaries, as
    movements of its service's base movement crediting its debt account (see
    service_account), their comment
    naming the service first (see DebtAccount.creditor_in). Never before the first date
    of the ledger or in the previous month.
    """
    bases, new_rows, now = service_bases(snapshot), [], snapshot.today
    services = services_of(snapshot)
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
                    "acc_debit": bases[service].acc_debit.name, "acc_credit": service_account(snapshot, services[service]),
                    "amount": f"{value:,.2f}", "comment": f"{service}: week {n}/{len(weeks)} of {month}",
                    ID_COLUMN: f"{SERVICE_ID}-{service}-{month}-{n}", SERVICE_COLUMN: service,
                })
    return _generated_rows(new_rows, [*snapshot.rows.columns, SERVICE_COLUMN], start)
