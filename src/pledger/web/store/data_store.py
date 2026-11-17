"""data_store.py: exporting every data file as a zip, and importing such a zip back."""

from __future__ import annotations

import datetime as dt
import io
import re
import tempfile
import zipfile
from dataclasses import dataclass, fields, replace
from pathlib import Path

from pledger.web.store.base_store import StoreBase
from pledger.web.store.common import LedgerDataError, _atomic_write

# Each data file of the Settings (an attribute of it) to its name in an export: its default
# path in info/, so that an export unzipped as info/ is a working data folder.
ARCHIVE_NAMES = {
    "accounts_path": "accounts.yaml",
    "base_movements_path": "result_types.yaml",
    "ledger_path": "movements/ledger.csv",
    "currencies_path": "currencies.yaml",
    "base_currency_path": "base_currency.csv",  # next to currencies.yaml (see Settings)
    "currency_values_path": "currencies.csv",  # next to currencies.yaml (see Settings)
    "investments_path": "investment.yaml",
    "creditors_path": "creditors.yaml",
    "work_path": "work.csv",
    "credit_cards_path": "credit_cards.yaml",
    "conversions_path": "movements/conversion.csv",
    "investment_movements_path": "movements/investment.csv",
    "interest_path": "movements/interest.csv",
    "prices_path": "movements/prices.csv",
    "services_path": "services.csv",
    "audits_path": "audits.csv",
    "budgets_path": "budgets.csv",
    "goals_path": "goals.csv",
    "trips_path": "trips.csv",
    "trip_items_path": "trip_items.csv",
    "carts_path": "carts.csv",
    "dashboard_path": "dashboard.yaml",
}
REQUIRED = ("accounts.yaml", "result_types.yaml", "movements/ledger.csv", "currencies.yaml")
MAX_FILE_SIZE = 50 * 1024 * 1024  # bytes, uncompressed: far more than years of movements
MAX_TOTAL_SIZE = 200 * 1024 * 1024
BACKUP_NAME = re.compile(r"[\w.-]+\.zip")


@dataclass(frozen=True)
class DataFile:
    """A data file of pledger: its name in an export, where it is, and its size and last change if it exists."""

    name: str
    path: Path
    size: int | None
    modified: dt.datetime | None


@dataclass(frozen=True)
class ImportResult:
    """What an import did: the files written, those removed (not in the zip), those of the zip left out, and the backup."""

    imported: list[str]
    removed: list[str]
    ignored: list[str]
    backup: Path


def _is_noise(name: str) -> bool:
    """The entries an archiver adds on its own, like those of macOS: not data files."""
    return name.startswith("__MACOSX/") or Path(name).name.startswith(".") or Path(name).name == "Thumbs.db"


def _strip_folder(names: list[str]) -> dict[str, str]:
    """
    Each name of a zip to its name as a data file: as it is, or without the folder all of
    them are in, if the data folder itself was zipped (e.g. `info/accounts.yaml`).
    """
    if any(n in ARCHIVE_NAMES.values() for n in names):
        return {n: n for n in names}
    tops = {n.split("/", 1)[0] for n in names}
    if len(tops) == 1 and all("/" in n for n in names):
        return {n: n.split("/", 1)[1] for n in names}
    return {n: n for n in names}


class DataStore(StoreBase):
    """
    Exports the data files as a zip, and imports one: only once the files of the zip load
    as a whole, saving the current ones to a backup first, which can be imported to undo it.
    """

    @property
    def backups_folder(self) -> Path:
        """Where the backups made before each import are: `backups/` next to the accounts YAML."""
        return self.settings.accounts_path.parent / "backups"

    def data_paths(self) -> dict[str, Path]:
        """The name in an export of each data file this pledger keeps (its path isn't None), to its path."""
        paths = {name: getattr(self.settings, attr) for attr, name in ARCHIVE_NAMES.items()}
        return {name: path for name, path in paths.items() if path is not None}

    def data_files(self) -> list[DataFile]:
        """Every data file (see data_paths), the missing ones too."""
        files = []
        for name, path in self.data_paths().items():
            try:
                stat = path.stat()
                files.append(DataFile(name, path, stat.st_size, dt.datetime.fromtimestamp(stat.st_mtime)))
            except OSError:
                files.append(DataFile(name, path, None, None))
        return files

    def export_zip(self) -> bytes:
        """The existing data files, as a zip with each one at its name (see ARCHIVE_NAMES)."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, path in self.data_paths().items():
                if path.is_file():
                    zf.write(path, name)
        return buffer.getvalue()

    def backups(self) -> list[DataFile]:
        """The backups made before the imports, newest first."""
        if not self.backups_folder.is_dir():
            return []
        found = [
            DataFile(p.name, p, (stat := p.stat()).st_size, dt.datetime.fromtimestamp(stat.st_mtime))
            for p in self.backups_folder.iterdir()
            if p.is_file() and BACKUP_NAME.fullmatch(p.name)
        ]
        return sorted(found, key=lambda b: (b.modified, b.name), reverse=True)

    def backup_path(self, name: str) -> Path | None:
        """The path of the backup of that name, if there is one."""
        return next((b.path for b in self.backups() if b.name == name), None)

    def _read_zip(self, data: bytes) -> tuple[dict[str, str], list[str]]:
        """
        The data files of a zip, by name, as text, and the names of the entries left out
        (unknown, or files this pledger doesn't keep). Raises ValueError if it can't be imported.
        """
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise ValueError("The file isn't a zip: import a zip exported from the Data tab") from None
        with zf:
            entries = [i for i in zf.infolist() if not i.is_dir() and not _is_noise(i.filename.replace("\\", "/"))]
            if sum(i.file_size for i in entries) > MAX_TOTAL_SIZE:
                raise ValueError(f"The zip is too big: more than {MAX_TOTAL_SIZE // 1024 // 1024} MB uncompressed")
            names = _strip_folder([i.filename.replace("\\", "/") for i in entries])
            kept = self.data_paths()
            files: dict[str, str] = {}
            ignored: list[str] = []
            for info in entries:
                name = names[info.filename.replace("\\", "/")]
                if name not in kept:
                    ignored.append(info.filename)
                    continue
                if name in files:
                    raise ValueError(f"The zip has {name} twice")
                if info.file_size > MAX_FILE_SIZE:
                    raise ValueError(f"{name} is too big: more than {MAX_FILE_SIZE // 1024 // 1024} MB")
                try:
                    files[name] = zf.read(info).decode("utf-8")
                except UnicodeDecodeError:
                    raise ValueError(f"{name} isn't a text file in UTF-8") from None
                except (zipfile.BadZipFile, OSError, RuntimeError) as e:  # corrupt, or encrypted
                    raise ValueError(f"{name} can't be read from the zip: {e}") from None
        missing = [n for n in REQUIRED if n not in files]
        if missing:
            raise ValueError(f"The zip is missing the required files {missing}: import a zip exported from the Data tab")
        return files, sorted(ignored)

    def _check_loads(self, files: dict[str, str]) -> None:
        """Load the files as pledger would, in a folder of their own: raises ValueError with what is wrong."""
        attrs = {f.name for f in fields(self.settings)}
        with tempfile.TemporaryDirectory(prefix="pledger-import-") as folder:
            root = Path(folder)
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, encoding="utf-8", newline="")
            settings = replace(self.settings, **{
                attr: root / name for attr, name in ARCHIVE_NAMES.items()
                if attr in attrs and getattr(self.settings, attr) is not None
            })
            settings.create_missing_files()
            store = type(self)(settings)
            try:
                store.load()
                store.load_work()
                store.load_dashboard()
                store.load_budgets()
                store.load_goals()
                store.load_trips()
                store.load_trip_items()
                store.load_carts()
                if settings.audits_path is not None:
                    store.load_audits()
            except LedgerDataError as e:
                raise ValueError(f"The data of the zip doesn't load, so nothing was imported: {e}") from None

    def import_zip(self, data: bytes) -> ImportResult:
        """
        Replace the data files with those of a zip exported from the Data tab, once they load
        (see _check_loads). The current files are saved to a backup first, and those missing
        from the zip are removed (or recreated empty, see Settings.create_missing_files),
        so the data is exactly that of the zip.
        """
        files, ignored = self._read_zip(data)
        self._check_loads(files)

        self.backups_folder.mkdir(parents=True, exist_ok=True)
        backup = self.backups_folder / f"before-import-{dt.datetime.now():%Y-%m-%d-%H%M%S}.zip"
        number = 1
        while backup.exists():
            number += 1
            backup = backup.with_name(f"{backup.stem.split('_')[0]}_{number}.zip")
        backup.write_bytes(self.export_zip())

        removed = []
        for name, path in self.data_paths().items():
            if name in files:
                _atomic_write(path, files[name])
            elif path.is_file():
                path.unlink()
                removed.append(name)
        self.settings.create_missing_files()
        return ImportResult(sorted(files), removed, ignored, backup)
