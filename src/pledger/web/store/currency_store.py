"""currency_store.py: the weekly values of the other currencies, in the currencies CSV."""

from __future__ import annotations

import csv
import datetime as dt
import io

from pandas import read_csv

from pledger.models import ESTIMATED
from pledger.web.store.base_store import StoreBase
from pledger.web.store.common import LedgerDataError, _existing, _parse_amount
from pledger.weeks import Week

CURRENCY_DATE = "date"  # the column of the dates of the currencies CSV


def value_text(value: float) -> str:
    """A value of a currency as written in the currencies CSV: at least 2 decimals, no thousands separators."""
    text = f"{value:.6f}".rstrip("0")
    whole, _, decimals = text.partition(".")
    return f"{whole}.{decimals.ljust(2, '0')}"


class CurrencyStore(StoreBase):
    """Sets the weekly values of the other currencies, in the base currency."""

    def save_currency_week(self, date: str, values: dict[str, str | float]) -> str:
        """
        Save the row of a date of the currencies CSV, with the value in the base currency
        of each other currency: an empty one is none, and ESTIMATED one to estimate (see
        Currency._load_values); the ones left out are emptied. It replaces the row of that
        date, else the one of its week with no known value (the one filled in on startup,
        see EstimateStore._fill_currencies), else it is a new row, in date order. The CSV
        is created if missing. The Foreign currency movements follow from the values.
        Returns the date of the row.
        """
        snapshot = self.load()
        path = self.settings.currency_values_path
        try:
            day = dt.date.fromisoformat(date.strip())
        except ValueError:
            raise ValueError(f"The date must be YYYY-MM-DD, not {date!r}") from None
        names = [c for c in snapshot.currencies if c != snapshot.base_currency.name]
        values = {c.strip(): str(v).strip() for c, v in values.items()}
        unknown = [c for c in values if c not in names]
        if unknown:
            raise ValueError(f"{unknown[0]!r} is not another currency of the currencies YAML")
        cells = {}
        for c, v in values.items():
            if v.lower() == ESTIMATED:
                cells[c] = ESTIMATED
            elif v:
                amount = _parse_amount(v, f"value of {c}")
                if amount <= 0:
                    raise ValueError(f"The value of {c} must be positive")
                cells[c] = value_text(amount)

        rows = []
        if _existing(path) and path.read_bytes().strip():
            frame = read_csv(path, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
            rows = [{k: str(v).strip() for k, v in r.items()} for r in frame.to_dict("records")]
        dates = [dt.date.fromisoformat(r[CURRENCY_DATE]) for r in rows]
        position = dates.index(day) if day in dates else next(
            (
                i for i, (d, r) in enumerate(zip(dates, rows))
                if Week.of(d) == Week.of(day)
                and all(v in ("", ESTIMATED) for k, v in r.items() if k != CURRENCY_DATE)
            ),
            None,
        )

        csv_file = self._read_file(len(rows), path, header=[CURRENCY_DATE, *names])
        before = path.read_bytes() if path.exists() else None
        lines = csv_file.lines
        header = next(csv.reader([lines[0]], delimiter=";"))
        for column in names:
            if column not in header:
                header.append(column)
                lines[0] += f";{column}"
        new_row = {CURRENCY_DATE: day.isoformat(), **cells}
        buf = io.StringIO()
        csv.writer(buf, delimiter=";", lineterminator="").writerow([new_row.get(c, "") for c in header])
        if position is not None:
            lines[csv_file.row_lines[position]] = buf.getvalue()
        else:
            # Right after the last earlier date, else before the first row
            earlier = [i for i, d in zip(csv_file.row_lines, dates) if d < day]
            lines.insert(max(earlier) + 1 if earlier else min(csv_file.row_lines, default=1), buf.getvalue())
        self._write_file(csv_file)
        # The values must still load (an estimate needs a known value before it): else it is undone
        try:
            self._load()
        except LedgerDataError as e:
            if before is None:
                path.unlink()
            else:
                path.write_bytes(before)
            raise ValueError(str(e).removeprefix("ValueError: ")) from None
        return day.isoformat()
