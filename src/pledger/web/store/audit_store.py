"""audit_store.py: the history of the audits, in the audits CSV."""

from __future__ import annotations

import datetime as dt
import io

from pandas import DataFrame, concat

from pledger.models import Audit, AuditEntry
from pledger.web.store.base_store import StoreBase
from pledger.web.store.common import (
    LedgerDataError,
    _atomic_write,
    _load_movements,
    stored_ids,
)
from pledger.web.store.constants import AUDIT_COLUMNS, LEDGER_ID


class AuditStore(StoreBase):
    """Reads the audits, adds them and marks their corrections. Without an audits CSV set, none are kept."""

    def _audit_rows(self) -> DataFrame:
        try:
            return _load_movements(self.settings.audits_path, AUDIT_COLUMNS, "audits")
        except (OSError, ValueError) as e:
            raise LedgerDataError(f"{type(e).__name__}: {e}") from e

    def _write_audit_rows(self, rows: DataFrame) -> None:
        text = io.StringIO()
        rows.to_csv(text, sep=";", index=False, lineterminator="\n")
        _atomic_write(self.settings.audits_path, text.getvalue())

    def load_audits(self) -> list[Audit]:
        """The audits made, oldest first (see Audit.from_rows)."""
        try:
            return Audit.from_rows(self._audit_rows())
        except (KeyError, ValueError) as e:
            raise LedgerDataError(f"The audits CSV is invalid: {type(e).__name__}: {e}") from e

    def add_audit(self, entries: list[AuditEntry]) -> int | None:
        """Save an audit, made now, with the next id: returns it (None if no audits CSV is set)."""
        if self.settings.audits_path is None:
            return None
        rows = self._audit_rows()
        audit = Audit(max((int(i) for i in rows["audit"]), default=0) + 1, dt.datetime.now().replace(microsecond=0), entries)
        new_rows = DataFrame(audit.rows(), columns=rows.columns, dtype=str).fillna("")
        self._write_audit_rows(concat([rows, new_rows], ignore_index=True) if len(rows) else new_rows)
        return audit.id

    def mark_correction(self, audit_id: str, account: str, base_movement: str, amount: float) -> bool:
        """
        Mark the correction just added to the ledger CSV, its last row, in the entry of
        this account in this audit. Returns whether there was one to mark.
        """
        if self.settings.audits_path is None:
            return False
        rows = self._audit_rows()
        entry = (rows["audit"] == str(audit_id).strip()) & (rows["account"] == account)
        if not entry.any():
            return False
        movement_id = stored_ids(self.load().rows, LEDGER_ID)[-1]
        rows.loc[entry, ["correction", "amount", "correction_id"]] = [base_movement, f"{amount:.2f}", movement_id or ""]
        self._write_audit_rows(rows)
        return True
