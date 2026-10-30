"""settings.py: where the data files are, and the display options."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pledger.models import INFLATION_COLUMN
from pledger.web.store.common import _header
from pledger.web.store.constants import (
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
            currency=os.environ.get("PLEDGER_CURRENCY", "$"),
            allowed_hosts=allowed_hosts(os.environ.get("PLEDGER_ALLOWED_HOSTS", "")),
        )

    def expected_files(self) -> dict[Path, str]:
        """
        Every data file, with what it is created with when missing: the YAMLs empty,
        the CSVs with just their header.
        """
        currencies_folder = self.currencies_path.parent
        files = {
            self.accounts_path: "",
            self.base_movements_path: "",
            self.ledger_path: _header([*LEDGER_CSV_COLUMNS, ID_COLUMN]),
            self.currencies_path: "",
            currencies_folder / "base_currency.csv": _header(["month", INFLATION_COLUMN]),
            currencies_folder / "currencies.csv": _header(["date"]),
            self.investments_path: "",
            self.creditors_path: "",
            self.work_path: _header(WORK_COLUMNS),
            self.credit_cards_path: "",
            self.conversions_path: _header([*CONVERSION_COLUMNS, ID_COLUMN]),
            self.investment_movements_path: _header([*INVESTMENT_COLUMNS, ID_COLUMN]),
            self.interest_path: _header([*INTEREST_COLUMNS, ID_COLUMN]),
            self.prices_path: _header(PRICE_COLUMNS),
            self.services_path: _header([SERVICE_MONTH]),
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
