"""settings.py: where the data files are, and the display options."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pledger.models import INFLATION_COLUMN
from pledger.web.store.common import _header
from pledger.web.store.constants import (
    AUDIT_COLUMNS,
    CONVERSION_COLUMNS,
    ID_COLUMN,
    INTEREST_COLUMNS,
    INVESTMENT_COLUMNS,
    LEDGER_CSV_COLUMNS,
    LOCAL_HOSTS,
    PRICE_COLUMNS,
    SERVICE_MONTH,
    WORK_COLUMNS,
)


def allowed_hosts(text: str) -> tuple[str, ...] | None:
    """The comma-separated hosts of PLEDGER_ALLOWED_HOSTS: LOCAL_HOSTS if empty, None (any) if `*`."""
    hosts = tuple(h.strip().lower().strip("[]") for h in text.split(",") if h.strip())
    if "*" in hosts:
        return None
    return hosts or LOCAL_HOSTS


@dataclass(frozen=True)
class Settings:
    """Paths to the ledger files and display options."""

    accounts_path: Path
    base_movements_path: Path
    ledger_path: Path
    currencies_path: Path
    investments_path: Path | None = None  # None or missing: no investment accounts
    creditors_path: Path | None = None  # None or missing: no debt accounts
    work_path: Path | None = None  # missing: no work history yet (created when adding a month)
    credit_cards_path: Path | None = None  # None or missing: no credit card accounts
    conversions_path: Path | None = None  # missing: no conversions yet (created when adding one)
    investment_movements_path: Path | None = None  # missing: none yet (created when adding one)
    interest_path: Path | None = None  # missing: no interest on money yet (created when adding some)
    prices_path: Path | None = None  # missing: no prices of the investments yet (created when adding one)
    services_path: Path | None = None  # missing: no monthly values of the services yet (created when adding one)
    audits_path: Path | None = None  # None: the audits aren't kept; missing: none yet (created with the first one)
    budgets_path: Path | None = None  # missing: no budgets yet (created when setting one)
    goals_path: Path | None = None  # missing: no saving goals yet (created when adding one)
    trips_path: Path | None = None  # missing: no trips yet (created when adding one)
    trip_items_path: Path | None = None  # missing: no planned expenses yet (created when adding one)
    carts_path: Path | None = None  # missing: no carts yet (created when adding one)
    currency: str = "$"
    allowed_hosts: tuple[str, ...] | None = LOCAL_HOSTS  # None: any host

    @classmethod
    def from_env(cls) -> Settings:
        """Read paths from PLEDGER_* env vars, defaulting to the info/ folder."""
        return cls(
            accounts_path=Path(os.environ.get("PLEDGER_ACCOUNTS", "info/accounts.yaml")),
            base_movements_path=Path(
                os.environ.get("PLEDGER_BASE_MOVEMENTS", "info/result_types.yaml")
            ),
            ledger_path=Path(os.environ.get("PLEDGER_LEDGER", "info/movements/ledger.csv")),
            currencies_path=Path(os.environ.get("PLEDGER_CURRENCIES", "info/currencies.yaml")),
            investments_path=Path(os.environ.get("PLEDGER_INVESTMENTS", "info/investment.yaml")),
            creditors_path=Path(os.environ.get("PLEDGER_CREDITORS", "info/creditors.yaml")),
            work_path=Path(os.environ.get("PLEDGER_WORK", "info/work.csv")),
            credit_cards_path=Path(os.environ.get("PLEDGER_CREDIT_CARDS", "info/credit_cards.yaml")),
            conversions_path=Path(os.environ.get("PLEDGER_CONVERSIONS", "info/movements/conversion.csv")),
            investment_movements_path=Path(
                os.environ.get("PLEDGER_INVESTMENT_MOVEMENTS", "info/movements/investment.csv")
            ),
            interest_path=Path(os.environ.get("PLEDGER_INTEREST", "info/movements/interest.csv")),
            prices_path=Path(os.environ.get("PLEDGER_PRICES", "info/movements/prices.csv")),
            services_path=Path(os.environ.get("PLEDGER_SERVICES", "info/services.csv")),
            audits_path=Path(os.environ.get("PLEDGER_AUDITS", "info/audits.csv")),
            budgets_path=Path(os.environ.get("PLEDGER_BUDGETS", "info/budgets.csv")),
            goals_path=Path(os.environ.get("PLEDGER_GOALS", "info/goals.csv")),
            trips_path=Path(os.environ.get("PLEDGER_TRIPS", "info/trips.csv")),
            trip_items_path=Path(os.environ.get("PLEDGER_TRIP_ITEMS", "info/trip_items.csv")),
            carts_path=Path(os.environ.get("PLEDGER_CARTS", "info/carts.csv")),
            currency=os.environ.get("PLEDGER_CURRENCY", "$"),
            allowed_hosts=allowed_hosts(os.environ.get("PLEDGER_ALLOWED_HOSTS", "")),
        )

    @property
    def base_currency_path(self) -> Path:
        """The monthly inflation of the base currency, next to the currencies YAML."""
        return self.currencies_path.parent / "base_currency.csv"

    @property
    def currency_values_path(self) -> Path:
        """The values of the other currencies, next to the currencies YAML."""
        return self.currencies_path.parent / "currencies.csv"

    def expected_files(self) -> dict[Path, str]:
        """
        Every data file of the Snapshot, with what it is created with when missing: the YAMLs
        empty, the CSVs with just their header. The plans (see PlanStore) aren't part of it:
        they are created when saving the first one, so they never stop pledger from starting.
        """
        files = {
            self.accounts_path: "",
            self.base_movements_path: "",
            self.ledger_path: _header([*LEDGER_CSV_COLUMNS, ID_COLUMN]),
            self.currencies_path: "",
            self.base_currency_path: _header(["month", INFLATION_COLUMN]),
            self.currency_values_path: _header(["date"]),
            self.investments_path: "",
            self.creditors_path: "",
            self.work_path: _header(WORK_COLUMNS),
            self.credit_cards_path: "",
            self.conversions_path: _header([*CONVERSION_COLUMNS, ID_COLUMN]),
            self.investment_movements_path: _header([*INVESTMENT_COLUMNS, ID_COLUMN]),
            self.interest_path: _header([*INTEREST_COLUMNS, ID_COLUMN]),
            self.prices_path: _header(PRICE_COLUMNS),
            self.services_path: _header([SERVICE_MONTH]),
            self.audits_path: _header(AUDIT_COLUMNS),
        }
        return {path: text for path, text in files.items() if path is not None}

    def create_missing_files(self) -> list[Path]:
        """Create the missing data files (see expected_files) and their folders. Returns those created."""
        created = []
        for path, text in self.expected_files().items():
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                created.append(path)
        return created
