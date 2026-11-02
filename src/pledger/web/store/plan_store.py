"""plan_store.py: the budgets, saving goals and trips: plans that never touch the ledger."""

from __future__ import annotations

import io
import re
from pathlib import Path

from pandas import DataFrame, concat

from pledger.models import AccountType
from pledger.web.store.base_store import StoreBase
from pledger.web.store.common import (
    LedgerDataError,
    _atomic_write,
    _check_name,
    _load_movements,
    _parse_amount,
    _parse_date,
    _value_on,
)
from pledger.web.store.constants import (
    BUDGET_COLUMNS,
    GOAL_COLUMNS,
    TRIP_COLUMNS,
    TRIP_ITEM_COLUMNS,
)


class PlanStore(StoreBase):
    """Reads and writes the budgets, goals and trips CSVs, each of them created with its first row."""

    @staticmethod
    def _plan_rows(path: Path | None, columns: list[str], what: str) -> DataFrame:
        try:
            return _load_movements(path, columns, what)
        except (OSError, ValueError) as e:
            raise LedgerDataError(f"{type(e).__name__}: {e}") from e

    @staticmethod
    def _write_plan(path: Path | None, rows: DataFrame, what: str) -> None:
        if path is None:
            raise ValueError(f"No {what} CSV is set")
        text = io.StringIO()
        rows.to_csv(text, sep=";", index=False, lineterminator="\n")
        _atomic_write(path, text.getvalue())

    @staticmethod
    def _with_row(rows: DataFrame, row: dict) -> DataFrame:
        new_row = DataFrame([row], columns=rows.columns, dtype=str).fillna("")
        return concat([rows, new_row], ignore_index=True) if len(rows) else new_row

    def load_budgets(self) -> DataFrame:
        return self._plan_rows(self.settings.budgets_path, BUDGET_COLUMNS, "budgets")

    def load_goals(self) -> DataFrame:
        return self._plan_rows(self.settings.goals_path, GOAL_COLUMNS, "goals")

    def load_trips(self) -> DataFrame:
        return self._plan_rows(self.settings.trips_path, TRIP_COLUMNS, "trips")

    def load_trip_items(self) -> DataFrame:
        return self._plan_rows(self.settings.trip_items_path, TRIP_ITEM_COLUMNS, "trip items")

    def _write_budgets(self, rows: DataFrame) -> None:
        self._write_plan(self.settings.budgets_path, rows, "budgets")

    def set_budget(self, month: str, base_movement: str, amount: str | float) -> None:
        """
        Budget this monthly amount for a loss base movement from `month` on, until a later
        month changes it, so past months keep the budget they had. Replaces that of `month`.
        """
        month = _check_month(month)
        if base_movement not in self.load().loss_base_movements():
            raise ValueError(f"Pick a loss base movement to budget, not {base_movement!r}")
        if not str(amount).strip():
            raise ValueError("The monthly amount is required")
        value = _parse_amount(amount, "amount")
        rows = self.load_budgets()
        rows = rows[~((rows["month"] == month) & (rows["base_movement"] == base_movement))]
        self._write_budgets(self._with_row(rows, {"month": month, "base_movement": base_movement, "amount": f"{value:.2f}"}))

    def stop_budget(self, month: str, base_movement: str) -> None:
        """Stop budgeting a base movement from `month` on: the months before it keep theirs."""
        month = _check_month(month)
        rows = self.load_budgets()
        of_base = rows["base_movement"] == base_movement
        earlier = (of_base & (rows["month"] < month)).any()
        rows = rows[~(of_base & (rows["month"] >= month))]
        if earlier:
            rows = self._with_row(rows, {"month": month, "base_movement": base_movement, "amount": "0.00"})
        self._write_budgets(rows)

    def save_goal(self, name: str, account: str, target: str | float, due: str = "") -> None:
        """
        Save a saving goal: reaching `target`, in the currency of an asset account, on that
        account, by the `due` date if any. A goal of the same name is replaced.
        """
        rows = self.load_goals()
        name = name.strip()
        rows = rows[rows["name"] != name]
        _check_name(name, {}, "goal")
        found = self.load().accounts.get(account)
        if found is None or found.acc_type != AccountType.ASSET:
            raise ValueError(f"Pick an asset account for the goal, not {account!r}")
        if not str(target).strip():
            raise ValueError("The target is required")
        value = _parse_amount(target, "target")
        if value == 0:
            raise ValueError("The target must be more than 0")
        date = _parse_date(due, "due date")
        row = {"name": name, "account": account, "target": f"{value:.2f}", "due": date.isoformat() if date else ""}
        self._write_plan(self.settings.goals_path, self._with_row(rows, row), "goals")

    def delete_goal(self, name: str) -> None:
        rows = self.load_goals()
        self._write_plan(self.settings.goals_path, rows[rows["name"] != name], "goals")

    def save_trip(self, name: str, start: str, end: str, base_movement: str = "") -> None:
        """
        Save a trip from `start` to `end`, whose expenses in the ledger are the movements of
        this loss base movement, if any (see views.trip_movements). A trip of the same name
        is updated, keeping its planned expenses.
        """
        name = name.strip()
        rows = self.load_trips()
        rows = rows[rows["name"] != name]
        _check_name(name, {}, "trip")
        first, last = _parse_date(start, "start date"), _parse_date(end, "end date")
        if first is None or last is None:
            raise ValueError("The start and end dates are required")
        if last < first:
            raise ValueError("The trip cannot end before it starts")
        if base_movement and base_movement not in self.load().loss_base_movements():
            raise ValueError(f"Pick a loss base movement for the trip, not {base_movement!r}")
        row = {"name": name, "start": first.isoformat(), "end": last.isoformat(), "base_movement": base_movement}
        self._write_plan(self.settings.trips_path, self._with_row(rows, row), "trips")

    def delete_trip(self, name: str) -> None:
        """Delete a trip and its planned expenses. The ledger stays as it is."""
        rows = self.load_trips()
        items = self.load_trip_items()
        self._write_plan(self.settings.trip_items_path, items[items["trip"] != name], "trip items")
        self._write_plan(self.settings.trips_path, rows[rows["name"] != name], "trips")

    def add_trip_item(self, trip: str, category: str, description: str, amount: str | float, currency: str) -> None:
        """Add a planned expense to a trip, in any currency that has a value to convert it with."""
        if trip not in set(self.load_trips()["name"]):
            raise ValueError(f"No trip named {trip!r}")
        snapshot = self.load()
        if currency not in snapshot.currencies:
            raise ValueError(f"Unknown currency: {currency!r}")
        _value_on(snapshot.currencies[currency], snapshot.today.isoformat(), "to plan an expense in it")
        if not str(amount).strip():
            raise ValueError("The amount is required")
        value = _parse_amount(amount, "amount")
        rows = self.load_trip_items()
        row = {
            "id": str(max((int(i) for i in rows["id"]), default=0) + 1), "trip": trip, "category": category.strip(),
            "description": re.sub(r"[;\r\n]+", ",", description).strip(), "amount": f"{value:.2f}", "currency": currency,
        }
        self._write_plan(self.settings.trip_items_path, self._with_row(rows, row), "trip items")

    def delete_trip_item(self, item_id: str) -> None:
        rows = self.load_trip_items()
        self._write_plan(self.settings.trip_items_path, rows[rows["id"] != item_id], "trip items")


def _check_month(month: str) -> str:
    month = month.strip()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError(f"Invalid month: {month!r}")
    return month
