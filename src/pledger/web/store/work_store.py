"""work_store.py: the months of the work history."""

from __future__ import annotations

import csv
import io

from pledger.models import WorkMonth, next_month, parse_month
from pledger.web.store.base_store import StoreBase
from pledger.web.store.common import (
    LedgerDataError,
    StaleRowError,
    _atomic_write,
    _existing,
    _parse_amount,
    _parse_date,
    work_fingerprint,
)
from pledger.web.store.constants import (
    COMPANY,
    GROSS,
    MONTH,
    PAYMENT_DATE,
    POSITION,
    WORK_COLUMNS,
)
from pledger.web.store.work import deduction_columns


class WorkStore(StoreBase):
    """Saves and deletes the months of the work CSV."""

    def save_month(
        self,
        month: str,
        company: str = "",
        position: str = "",
        gross: str | float = "",
        deductions: dict[str, str | float] | None = None,
        payment_date: str = "",
    ) -> bool:
        """
        Save a month of the work history: replacing it if it is already there, else
        adding it right after the last month or right before the first one, so there
        are no missing months. Without a company, it is a gap (no job). The deductions
        of the salary are by the name of their loss base movement, 0 if left out; a
        new one adds its column, 0 for every other month with a job. Returns whether
        the month is new. Creates the work CSV if missing.
        """
        path = self.settings.work_path
        if path is None:
            raise ValueError("No work CSV is set")
        work = self.load_work()
        texts = {"company": company, "position": position}
        for what, text in texts.items():
            if any(c in str(text) for c in "\r\n"):
                raise ValueError(f"The {what} cannot contain line breaks")
        deductions = {str(n).strip(): v for n, v in (deductions or {}).items()}
        reserved = [n for n in deductions if n in WORK_COLUMNS or not n or any(c in n for c in "\r\n;\"")]
        if reserved:
            raise ValueError(f"Invalid deduction of the salary: {reserved[0]!r}")
        entry = WorkMonth(
            month=parse_month(month, "month"),
            company=str(company).strip() or None,
            position=str(position).strip() or None,
            gross=_parse_amount(gross, "gross salary") if str(gross).strip() else None,
            deductions={
                n: _parse_amount(v, f"deduction {n!r}") for n, v in deductions.items() if str(v).strip()
            },
            payment_date=_parse_date(payment_date, "received payment date"),
        )

        if not _existing(path) or not path.read_bytes().strip():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(";".join(WORK_COLUMNS) + "\n", encoding="utf-8")
            work = self.load_work()
        months = [m.month for m in work.months]
        if entry.month in months:
            index = months.index(entry.month)
            old_row = dict(work.rows.iloc[index])
        elif not months or entry.month == next_month(months[-1]):
            index, old_row = len(months), {}
        elif next_month(entry.month) == months[0]:
            index, old_row = 0, {}
        else:
            expected = f"{next_month(months[-1])} (after the last month)"
            raise ValueError(
                f"Months cannot be skipped: add {expected} first, leaving the company "
                "empty for months without a job"
            )

        before = path.read_bytes().decode("utf-8")
        csv_file = self._read_file(len(work.rows), path)
        columns = list(work.rows.columns)
        new_columns = [n for n in entry.deductions if n not in columns and entry.deductions[n]]
        if entry.payment_date and PAYMENT_DATE not in columns:
            new_columns.append(PAYMENT_DATE)
        for column in new_columns:
            # Add the column: empty for every other month, 0 for the deductions of the worked ones.
            columns.append(column)
            csv_file.lines[0] += ";" + column
            for i, other in zip(csv_file.row_lines, work.months):
                csv_file.lines[i] += ";" + ("0.00" if column != PAYMENT_DATE and not other.is_gap else "")
        new_row = {col: old_row.get(col, "") for col in columns}
        new_row.update({
            MONTH: entry.month,
            COMPANY: entry.company or "",
            POSITION: entry.position or "",
            GROSS: "" if entry.gross is None else f"{entry.gross:.2f}",
        })
        for column in deduction_columns(columns):
            amount = entry.deductions.get(column, 0.0)
            new_row[column] = "" if entry.is_gap else f"{amount:.2f}"
        if PAYMENT_DATE in columns:
            new_row[PAYMENT_DATE] = entry.payment_date.isoformat() if entry.payment_date else ""
        buf = io.StringIO()
        csv.writer(buf, delimiter=";", lineterminator="").writerow(new_row.values())
        if old_row:
            csv_file.lines[csv_file.row_lines[index]] = buf.getvalue()
        elif index == 0 and months:
            csv_file.lines.insert(csv_file.row_lines[0], buf.getvalue())
        else:
            csv_file.lines.append(buf.getvalue())
        self._write_file(csv_file)
        try:
            self.load_work()
            if any(c != PAYMENT_DATE for c in new_columns):
                self.load()  # a new deduction must be of a valid base movement
        except LedgerDataError as e:
            _atomic_write(path, before)
            raise ValueError(f"Could not save {entry.month}: {e}") from None
        return not old_row

    def delete_month(self, index: int, fingerprint: str | None = None) -> None:
        """
        Delete the first or the last month of the work history by position, optionally
        checking it is the expected one. Other months can only be made gaps.
        """
        rows = self.load_work().rows
        if not 0 <= index < len(rows):
            raise StaleRowError(f"Work row {index} does not exist")
        if fingerprint is not None and work_fingerprint(rows.iloc[index]) != fingerprint:
            raise StaleRowError("The work history changed since this page was loaded; reload and try again")
        if index not in {0, len(rows) - 1}:
            raise ValueError(
                "Only the first or last month can be deleted; leave the company of the others "
                "empty to make them months without a job"
            )
        csv_file = self._read_file(len(rows), self.settings.work_path)
        del csv_file.lines[csv_file.row_lines[index]]
        self._write_file(csv_file)
