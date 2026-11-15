"""dashboard_store.py: what each slot of the Dashboard tab shows, kept in its own YAML."""

from __future__ import annotations

import yaml

from pledger.web.store.base_store import StoreBase
from pledger.web.store.common import LedgerDataError, _atomic_write

# The slots of the dashboard, by kind: number cards and plots
DASHBOARD_SLOTS = {"cards": 8, "plots": 6}
# What a dashboard shows until it is changed: a few of each kind, the rest free
DEFAULT_DASHBOARD = {
    "cards": ["equity", "month-profit", "month-losses", "months-covered"],
    "plots": ["equity-over-time", "monthly-profit"],
}

Dashboard = dict[str, list[str | None]]


def _slots(widgets: object, size: int) -> list[str | None]:
    """A list of `size` widget names (None for a free slot), padded or cut from `widgets`."""
    names = [w if isinstance(w, str) and w else None for w in widgets] if isinstance(widgets, list) else []
    return (names + [None] * size)[:size]


class DashboardStore(StoreBase):
    """
    Reads and writes the dashboard YAML: a list of widget names (or null, a free slot) per
    kind of slot. Without a dashboard_path it is only kept while pledger runs.
    """

    _dashboard: Dashboard | None = None  # the one kept without a file

    def load_dashboard(self) -> Dashboard:
        path = self.settings.dashboard_path
        data: object = self._dashboard if path is None else None
        if path is not None and path.exists():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as e:
                raise LedgerDataError(f"Could not read the dashboard {path}: {e}") from e
        if not isinstance(data, dict):
            data = DEFAULT_DASHBOARD
        return {kind: _slots(data.get(kind), size) for kind, size in DASHBOARD_SLOTS.items()}

    def save_dashboard(self, dashboard: Dashboard) -> None:
        dashboard = {kind: _slots(dashboard.get(kind), size) for kind, size in DASHBOARD_SLOTS.items()}
        path = self.settings.dashboard_path
        if path is None:
            self._dashboard = dashboard
            return
        text = "# The widgets of each slot of the Dashboard tab (null: a free slot), set from the tab\n"
        _atomic_write(path, text + yaml.safe_dump(dashboard, sort_keys=False))
