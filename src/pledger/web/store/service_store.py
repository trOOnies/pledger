"""service_store.py: the monthly values of the services of the debt account."""

from __future__ import annotations

import csv
import io
from dataclasses import replace

from pandas import DataFrame, concat

from pledger.models import ESTIMATED, parse_month
from pledger.web.store.base_store import StoreBase
from pledger.web.store.common import _parse_amount
from pledger.web.store.constants import SERVICE_MONTH
from pledger.web.store.services import (
    service_bases,
    service_cells,
    service_estimates,
    service_months,
    service_payment_base,
)


class ServiceStore(StoreBase):
    """Sets the monthly values of the services CSV, and pays the services."""

    def add_service_payment(
        self, service: str, month: str, move_date: str, acc_credit: str, amount: str | float, set_value: bool = False,
    ) -> str:
        """
        Pay a service its value of a month (see service_payment_base): a movement of the
        Debt tab debiting its debt account and crediting `acc_credit`, about it, commented
        with the month. A known value of the month is never changed, even if the payment
        differs from it: what remains is balanced later. With `set_value`, only for a
        month whose value isn't known (estimated or empty), the amount becomes its value.
        Returns the base movement of the payment.
        """
        snapshot = self.load()
        service, month = service.strip(), parse_month(month, "month")
        if service not in service_bases(snapshot):
            raise ValueError(f"{service!r} is not a service with a base movement in the creditors YAML")
        known = service not in service_estimates(snapshot).get(month, []) and service in dict(service_months(snapshot)).get(month, {})
        if set_value and known:
            raise ValueError(f"The value of {service} in {month} is already known: it isn't replaced by a payment")
        base, debt = service_payment_base(snapshot, service)
        self.add_debt_movement(move_date, base, debt, acc_credit, amount, service, f"payment of {month}")
        if set_value:
            self.set_service_value(month, service, amount)
        return base

    def set_service_value(self, month: str, service: str, amount: str | float = "") -> None:
        """Set the value of a service in a month of the services CSV, keeping the others (see save_service_month)."""
        snapshot = self.load()
        month = parse_month(month, "month")
        self.save_service_month(month, {**service_cells(snapshot).get(month, {}), service.strip(): amount})

    def save_service_month(self, month: str, values: dict[str, str | float]) -> None:
        """
        Save the row of a month of the services CSV (see _service_rows), replacing it, with
        the value of each service (an empty one is none, and ESTIMATED one to estimate);
        the ones left out are emptied.
        A month not in the CSV yet gets its row, in month order, and a service not in it
        yet with a value gets its column. The CSV is created if missing.
        """
        snapshot = self.load()
        path = self.settings.services_path
        if path is None:
            raise ValueError("No services CSV is set")
        month = parse_month(month, "month")
        known = service_bases(snapshot)
        values = {c.strip(): str(v).strip() for c, v in values.items()}
        unknown = [c for c in values if c not in known]
        if unknown:
            raise ValueError(f"{unknown[0]!r} is not a service with a base movement in the creditors YAML")
        cells = {
            c: v if v == ESTIMATED else f"{_parse_amount(v, f'value of {c}'):,.2f}"
            for c, v in values.items() if v
        }
        rows = snapshot.services
        months = [m.strip() for m in rows[SERVICE_MONTH]]
        new_row = {SERVICE_MONTH: month, **cells}
        others = rows[[m != month for m in months]]
        changed = concat([others, DataFrame([new_row])], ignore_index=True).fillna("")
        replace(snapshot, services=changed).ledger().check_accounts()

        csv_file = self._read_file(len(rows), path, header=[SERVICE_MONTH])
        lines = csv_file.lines
        header = next(csv.reader([lines[0]], delimiter=";"))
        for column in cells:
            if column not in header:
                header.append(column)
                lines[0] += f";{column}"
        buf = io.StringIO()
        csv.writer(buf, delimiter=";", lineterminator="").writerow([new_row.get(c, "") for c in header])
        if month in months:
            lines[csv_file.row_lines[months.index(month)]] = buf.getvalue()
        else:
            # Right after the last earlier month, else before the first row
            earlier = [i for i, m in zip(csv_file.row_lines, months) if m < month]
            lines.insert(max(earlier) + 1 if earlier else min(csv_file.row_lines, default=1), buf.getvalue())
        self._write_file(csv_file)

