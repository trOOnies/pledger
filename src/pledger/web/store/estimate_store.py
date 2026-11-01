"""estimate_store.py: filling in the weeks and months that went by without a value."""

from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path

from pandas import read_csv

from pledger.models import ESTIMATED, INFLATION_COLUMN, next_month, parse_month
from pledger.web.store import common
from pledger.web.store.base_store import StoreBase
from pledger.web.store.common import _existing
from pledger.web.store.constants import SERVICE_MONTH
from pledger.weeks import Week


class EstimateStore(StoreBase):
    """Adds ESTIMATED rows to the CSVs of values, for what hasn't been written down yet."""

    def fill_estimated(self) -> None:
        """
        Add a row to the base currency, currencies and services CSVs for each month or
        week missing from them, from their first one up to the current one, with ESTIMATED
        (see Currency.load_yaml and service_months). Run on startup. Every other byte of
        the files is kept, and a CSV without rows has nothing to estimate from.
        """
        s = self.settings
        self._fill_inflation(s.base_currency_path)
        self._fill_currencies(s.currency_values_path)
        if s.services_path is not None:
            self._fill_services(s.services_path)

    def _rows(self, path: Path) -> list[dict[str, str]]:
        """The rows of a CSV, stripped; none if it is missing or empty."""
        if not _existing(path) or not path.read_bytes().strip():
            return []
        rows = read_csv(path, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
        return [{c: v.strip() for c, v in row.items()} for row in rows.fillna("").to_dict("records")]

    def _missing_months(self, rows: list[dict[str, str]], column: str, what: str) -> list[str]:
        """The months from the first one of these rows up to the current one that they don't have."""
        months = {parse_month(row[column], f"month of the {what} CSV") for row in rows}
        current, found = common.today().strftime("%Y-%m"), []
        month = min(months)
        while month <= current:
            if month not in months:
                found.append(month)
            month = next_month(month)
        return found

    def _fill_inflation(self, path: Path) -> None:
        rows = self._rows(path)
        if rows:
            missing = self._missing_months(rows, "month", "base currency")
            self._insert_rows(path, "month", len(rows), {m: {INFLATION_COLUMN: ESTIMATED} for m in missing})

    def _fill_currencies(self, path: Path) -> None:
        """Every currency with a value before a missing week gets it; the week is on its Monday."""
        rows = self._rows(path)
        if not rows:
            return
        dated = sorted((dt.date.fromisoformat(row["date"]), row) for row in rows)
        weeks, new_rows = {Week.of(day) for day, _ in dated}, {}
        week, current = Week.of(dated[0][0]), Week.of(common.today())
        while week <= current:
            if week not in weeks:
                started = {c for day, row in dated if day < week.start for c, v in row.items() if c != "date" and v}
                if started:
                    new_rows[week.start.isoformat()] = dict.fromkeys(started, ESTIMATED)
            week = Week(week.start + dt.timedelta(days=7))
        self._insert_rows(path, "date", len(rows), new_rows)

    def _fill_services(self, path: Path) -> None:
        """A missing month estimates the services of the month before it: an empty cell is none."""
        rows = self._rows(path)
        if not rows:
            return
        months = {row[SERVICE_MONTH]: row for row in rows}
        new_rows = {}
        for month in self._missing_months(rows, SERVICE_MONTH, "services"):
            before = months[max(m for m in months if m < month)]
            cells = {c: ESTIMATED for c, v in before.items() if c != SERVICE_MONTH and v}
            months[month] = cells
            if cells:
                new_rows[month] = cells
        self._insert_rows(path, SERVICE_MONTH, len(rows), new_rows)

    def _insert_rows(self, path: Path, key: str, n_rows: int, new_rows: dict[str, dict[str, str]]) -> None:
        """Insert these rows, by the value of their `key` column, each after the last row before it."""
        if not new_rows:
            return
        csv_file = self._read_file(n_rows, path)
        lines = csv_file.lines
        header = next(csv.reader([lines[0]], delimiter=";"))
        keys = [next(csv.reader([lines[i]], delimiter=";"))[header.index(key)].strip() for i in csv_file.row_lines]
        for value in sorted(new_rows, reverse=True):  # so that the earlier positions stay valid
            earlier = [i for i, k in zip(csv_file.row_lines, keys) if k < value]
            buf = io.StringIO()
            row = {key: value, **new_rows[value]}
            csv.writer(buf, delimiter=";", lineterminator="").writerow([row.get(c, "") for c in header])
            lines.insert(max(earlier) + 1 if earlier else csv_file.row_lines[0], buf.getvalue())
        self._write_file(csv_file)
