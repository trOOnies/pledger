"""service_store.py: the monthly values of the services of the debt account."""

from __future__ import annotations

import csv
import io
from dataclasses import replace

from pandas import DataFrame, concat

from pledger.models import DEBT_ACCOUNT, parse_month
from pledger.web.store.base_store import StoreBase
from pledger.web.store.common import _parse_amount
from pledger.web.store.constants import SERVICE_MONTH
from pledger.web.store.services import service_bases, service_months


class ServiceStore(StoreBase):
    """Sets the monthly values of the services CSV."""

    def set_service_value(self, month: str, service: str, amount: str | float = "") -> None:
        """Set the value of a service in a month of the services CSV, keeping the others (see save_service_month)."""
        snapshot = self.load()
        month = parse_month(month, "month")
        values = dict(service_months(snapshot)).get(month, {})
        self.save_service_month(month, {**{c: f"{v:,.2f}" for c, v in values.items()}, service.strip(): amount})

    def save_service_month(self, month: str, values: dict[str, str | float]) -> None:
        """
        Save the row of a month of the services CSV (see _service_rows), replacing it, with
        the value of each service (an empty one is none); the ones left out are emptied.
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
            raise ValueError(f"{unknown[0]!r} is not a service of {DEBT_ACCOUNT} with a base movement in the creditors YAML")
        cells = {
            c: f"{_parse_amount(v, f'value of {c}'):,.2f}" for c, v in values.items() if v
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
