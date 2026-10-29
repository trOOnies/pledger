"""ledger_store.py: reading and writing the data files."""

from __future__ import annotations

import csv
import datetime as dt
import io
import math
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pandas import DataFrame, Series, concat, read_csv

from pledger.enums import AccountType, InvestmentType, MovementType
from pledger.models import (
    DEBT_ACCOUNT,
    INTERNAL,
    SPECIAL_SIDES,
    Account,
    CreditCardAccount,
    Currency,
    DebtAccount,
    InvestmentAccount,
    MovementBase,
    WorkMonth,
    load_previous_month,
    next_month,
    parse_month,
)
from pledger.web.store import common
from pledger.web.store.common import (
    LedgerDataError,
    StaleRowError,
    _atomic_write,
    _check_description,
    _check_name,
    _CsvFile,
    _existing,
    _fit_row,
    _load_movements,
    _parse_amount,
    _parse_date,
    _quantity_text,
    _value_on,
    _with_row,
    conversion_fingerprint,
    interest_fingerprint,
    next_id,
    row_fingerprint,
    stored_ids,
    work_fingerprint,
)
from pledger.web.store.constants import (
    BOM,
    COMPANY,
    CONVERSION_COLUMNS,
    CONVERSION_ID,
    CORRECTION_GAIN,
    CORRECTION_LOSS,
    CROSS_CONVERSION,
    GENERATED,
    GROSS,
    ID_COLUMN,
    INITIAL_PRICE,
    INTEREST_COLUMNS,
    INTEREST_ID,
    INVESTMENT_COLUMN,
    INVESTMENT_COLUMNS,
    INVESTMENT_ID,
    LEDGER_ID,
    MONTH,
    PAYMENT_DATE,
    POSITION,
    PRICE_COLUMNS,
    SERVICE_MONTH,
    WORK_COLUMNS,
)
from pledger.web.store.conversions import (
    _conversion_row,
    _cross_rows,
    _exchange_args,
    conversion_pairs,
    is_cross,
)
from pledger.web.store.investments import (
    _check_invest,
    _check_known_investment,
    initial_prices,
)
from pledger.web.store.movements import (
    _check_initial_base_movements,
    _check_tab,
    _debt_comment,
    _movement_row,
)
from pledger.web.store.services import service_columns, service_months
from pledger.web.store.snapshot import Snapshot
from pledger.web.store.work import Work, _work_month, deduction_columns
from pledger.web.store.yaml_edit import (
    _account_fields,
    _insert_yaml_entry,
    _replace_yaml_entry,
)

if TYPE_CHECKING:
    from pledger.web.store.settings import Settings


class LedgerStore:
    """Reads and writes the ledger files. The files are the source of truth."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._cached: tuple[tuple, Snapshot] | None = None  # see load

    def _files_state(self) -> tuple:
        """What tells whether the data files changed: their mtime, size and inode, and today."""
        state = []
        for path in self.settings.expected_files():
            try:
                stat = path.stat()
                state.append((str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino))
            except OSError:
                state.append((str(path), None))
        return (common.today(), *state)

    def load(self) -> Snapshot:
        """
        The snapshot of the data files, reused while none of them change (see _files_state):
        nothing changes a snapshot once loaded, so the requests of a page can share it.
        """
        state = self._files_state()
        if self._cached is not None and self._cached[0] == state:
            return self._cached[1]
        snapshot = self._load()
        self._cached = (state, snapshot)
        return snapshot

    def _load(self) -> Snapshot:
        s = self.settings
        try:
            base_currency, other_currencies = Currency.load_yaml(str(s.currencies_path))
            currencies = {base_currency.name: base_currency, **other_currencies}
            work = self.load_work()
            prices = _load_movements(s.prices_path, PRICE_COLUMNS, "prices")
            services = _load_movements(s.services_path, [SERVICE_MONTH], "services")
            accounts = Account.load_yaml(
                str(s.accounts_path), base_currency, other_currencies,
                _existing(s.investments_path), _existing(s.creditors_path),
                list(work.companies), _existing(s.credit_cards_path), initial_prices(prices),
            )
            base_movements, result_types = MovementBase.load_yaml(
                str(s.base_movements_path), accounts
            )
            _check_initial_base_movements(base_movements)
            rows = read_csv(
                s.ledger_path, sep=";", dtype=str, keep_default_na=False,
                encoding="utf-8-sig",
            )
            stored = [n for n in GENERATED if n in set(rows["base_movement"])]
            if stored:
                raise ValueError(
                    f"The ledger CSV cannot have movements of {stored}: they are generated from the "
                    "initial amounts of the accounts, the values of the currencies and the work history"
                )
            snapshot = Snapshot(
                accounts, base_movements, result_types, rows, currencies, self.load_conversions(),
                _load_movements(s.investment_movements_path, INVESTMENT_COLUMNS, "investments"),
                _load_movements(s.interest_path, INTEREST_COLUMNS, "interest"),
                prices, services, work, load_previous_month(str(s.accounts_path)),
            )
            misplaced = sorted(set(rows["base_movement"]) & set(snapshot.invest_base_movements()))
            if misplaced:
                raise ValueError(
                    f"The ledger CSV cannot have movements of {misplaced}: they go in the investments "
                    "CSV, or in the interest CSV for the interest on money"
                )
            snapshot.ledger().check_accounts()  # validate the whole ledger up front
            snapshot.investment_accounts()
        except (OSError, KeyError, ValueError, AssertionError) as e:
            raise LedgerDataError(f"{type(e).__name__}: {e}") from e
        return snapshot

    def freeze_ids(self) -> None:
        """
        Give a new id to each row of the CSVs of movements without a valid one (see
        stored_ids), adding their ID_COLUMN if missing. Run on startup and when
        adding a movement to a CSV, so the rows added to the CSVs by hand get theirs too.
        """
        s = self.settings
        for path, prefix in (
            (s.ledger_path, LEDGER_ID),
            (s.conversions_path, CONVERSION_ID),
            (s.investment_movements_path, INVESTMENT_ID),
            (s.interest_path, INTEREST_ID),
        ):
            if _existing(path) and path.read_bytes().strip():
                self._freeze_file(path, prefix)

    def _freeze_file(self, path: Path, prefix: str) -> None:
        rows = read_csv(path, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
        ids = stored_ids(rows, prefix)
        if ID_COLUMN in rows.columns and all(ids):
            return
        csv_file = self._read_file(len(rows), path)
        lines = csv_file.lines
        header = next(csv.reader([lines[0]], delimiter=";"))
        if ID_COLUMN not in header:
            header.append(ID_COLUMN)
            lines[0] += f";{ID_COLUMN}"
        col = header.index(ID_COLUMN)
        number = int(next_id(rows, prefix).split("-")[1])
        for i, current in zip(csv_file.row_lines, ids):
            if current:
                continue
            new, number = f"{prefix}-{number}", number + 1
            fields = next(csv.reader([lines[i]], delimiter=";"))
            if len(fields) <= col == len(header) - 1:
                # The id is the last column and the line stops before it: keep its bytes.
                lines[i] += ";" * (col - len(fields) + 1) + new
            else:
                fields += [""] * (len(header) - len(fields))
                fields[col] = new
                buf = io.StringIO()
                csv.writer(buf, delimiter=";", lineterminator="").writerow(fields)
                lines[i] = buf.getvalue()
        self._write_file(csv_file)

    def add(
        self,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount: str | float,
        comment: str = "",
        invest: bool = False,
        investment: str = "",
        quantity: str | float = "",
        debt: bool = False,
        card_payment: bool = False,
        correction: bool = False,
    ) -> None:
        """
        Validate a new movement and append it to its CSV. With `invest` (from the
        Investments tab), it must be of an investment base movement (see
        Snapshot.invest_base_movements): the interest on money goes to the interest CSV,
        and the others to the investments CSV, with their investment and quantity (see
        _investment_details); an investment of an investment account must be one it has
        (see _check_known_investment). Without it, it must be of a non-internal base
        movement whose movements aren't generated, and goes to the ledger CSV: one of a
        debt base movement with `debt` (from the Debt tab), one of a card payment base
        movement with `card_payment` (from the Credit Cards tab), a balance correction with
        `correction` (from the Accounts tab), else one of the Ledger tab (see _check_tab). It gets the next id of its CSV (see _appended).
        """
        snapshot = self.load()
        field, new_row = _movement_row(
            snapshot, move_date, base_movement, acc_debit, acc_credit, amount, comment,
            invest, investment, quantity, debt, card_payment, correction,
        )
        path, what, prefix = {
            "rows": (self.settings.ledger_path, "ledger", LEDGER_ID),
            "interest": (self.settings.interest_path, "interest", INTEREST_ID),
            "investments": (self.settings.investment_movements_path, "investments", INVESTMENT_ID),
        }[field]
        if path is None:
            raise ValueError(f"No {what} CSV is set")
        frame = getattr(snapshot, field)
        new_row = _fit_row(new_row, frame.columns, what)
        changed = replace(snapshot, **{field: concat([frame, DataFrame([new_row])], ignore_index=True)})
        changed.ledger().check_accounts()
        changed.investment_accounts()  # e.g. not selling more than is held
        if field == "investments":
            _check_known_investment(snapshot, acc_debit, acc_credit, new_row[INVESTMENT_COLUMN])
        self._write_file(self._appended(path, prefix, new_row, list(frame.columns)))

    def add_investment(
        self, account: str, name: str, investment_type: str, quantity: str | float = "", price: str | float = ""
    ) -> None:
        """
        Add an investment to an investment account in the investments YAML, with its
        type and, optionally, its initial quantity (0 by default), which needs its
        initial price, added to the prices CSV (see Account.load_yaml). An investment
        only in the ledger gets its type this way; one in the YAML already has it.
        """
        snapshot = self.load()
        path, prices_path = self.settings.investments_path, self.settings.prices_path
        if path is None:
            raise ValueError("No investments YAML is set")
        accounts = {a.name: a for a in snapshot.accounts.values() if isinstance(a, InvestmentAccount)}
        if account not in accounts:
            raise ValueError(f"{account!r} is not an investment account")
        name = _check_name(name, accounts[account].investments, "investment")
        try:
            inv_type = InvestmentType(investment_type)
        except ValueError:
            raise ValueError(f"Unknown investment type: {investment_type!r}") from None
        units = _parse_amount(quantity, "initial quantity") if str(quantity).strip() else 0.0
        value = _parse_amount(price, "initial price") if str(price).strip() else None
        if units and value is None:
            raise ValueError("An initial quantity needs its initial price")
        if value is not None and prices_path is None:
            raise ValueError("No prices CSV is set")
        fields: str | dict = {"type": inv_type.value, "quantity": units} if units else inv_type.value
        before = path.read_bytes().decode("utf-8") if path.exists() else None
        prices_before = prices_path.read_bytes() if prices_path is not None and prices_path.exists() else None
        _atomic_write(path, _insert_yaml_entry(before or "", (account,), name, fields))
        try:
            if value is not None:
                self._append_price(snapshot, INITIAL_PRICE, account, name, value)
            self.load()
        except (LedgerDataError, ValueError) as e:
            if before is None:
                path.unlink()
            else:
                _atomic_write(path, before)
            if value is not None and prices_before is None:
                prices_path.unlink(missing_ok=True)
            elif value is not None:
                _atomic_write(prices_path, prices_before.decode("utf-8"))
            raise ValueError(f"Could not add {name!r}: {e}") from None

    def add_price(self, date: str, account: str, investment: str, price: str | float) -> None:
        """
        Add a price of an investment of an investment account on a date to the prices
        CSV: it is revalued at it at the end of that date (see _capital_rows). An
        investment has one price on each date at most.
        """
        snapshot = self.load()
        if _parse_date(date, "date") is None:
            raise ValueError("The date is required")
        accounts = {a.name: a for a in snapshot.investment_accounts()}
        if account not in accounts:
            raise ValueError(f"{account!r} is not an investment account")
        investment = investment.strip()
        if investment not in accounts[account].investments:
            raise ValueError(f"{investment!r} is not an investment of {account}: add it as a new investment first")
        value = _parse_amount(price, "price")
        date = date.strip()
        taken = zip(snapshot.prices["date"], snapshot.prices["account"], snapshot.prices[INVESTMENT_COLUMN])
        if (date, account, investment) in {(d.strip(), a.strip(), n.strip()) for d, a, n in taken}:
            raise ValueError(f"{investment} of {account} already has a price on {date}: change it in the prices CSV")
        new_row = {"date": date, "account": account, INVESTMENT_COLUMN: investment, "price": _quantity_text(value)}
        changed = replace(snapshot, prices=concat([snapshot.prices, DataFrame([new_row])], ignore_index=True))
        changed.ledger().check_accounts()
        changed.investment_accounts()
        self._append_price(snapshot, date, account, investment, value)

    def _append_price(self, snapshot: Snapshot, date: str, account: str, investment: str, price: float) -> None:
        """Append a price to the prices CSV, created with its header if missing."""
        path = self.settings.prices_path
        if path is None:
            raise ValueError("No prices CSV is set")
        csv_file = self._read_file(len(snapshot.prices), path, header=PRICE_COLUMNS)
        header = next(csv.reader([csv_file.lines[0]], delimiter=";"))
        values = {"date": date, "account": account, INVESTMENT_COLUMN: investment, "price": _quantity_text(price)}
        buf = io.StringIO()
        csv.writer(buf, delimiter=";", lineterminator="").writerow([values.get(c, "") for c in header])
        csv_file.lines.append(buf.getvalue())
        self._write_file(csv_file)

    def add_account(
        self,
        name: str,
        acc_type: str,
        description: str,
        initial_amount: str | float,
        currency: str = "",
        is_liquid: bool = False,
        initial_date: str = "",
        checks: Sequence[str] = (),
    ) -> None:
        """
        Validate a new account and add it to the accounts YAML. Without a currency,
        it is in the base currency; without an initial date, it starts on the
        first date of the ledger. Its initial amount is in its currency. Its initial
        movement is generated when loading, and must not break its `checks`
        (AccountCheck values) either.
        """
        snapshot = self.load()
        name = _check_name(name, snapshot.accounts, "account")
        if name in SPECIAL_SIDES:
            raise ValueError(f"{name!r} is reserved and cannot be an account name")
        try:
            acc_type = AccountType(acc_type)
        except ValueError:
            raise ValueError(f"Invalid account type: {acc_type!r}") from None
        if acc_type == AccountType.EQUITY:
            raise ValueError("New equity accounts are not supported")
        fields = _account_fields(snapshot, description, initial_amount, currency, is_liquid, initial_date, checks)
        self._add_yaml_entry(self.settings.accounts_path, (acc_type.value,), name, fields)

    def edit_account(
        self,
        name: str,
        description: str,
        initial_amount: str | float = "",
        is_liquid: bool = False,
        initial_date: str = "",
        checks: Sequence[str] = (),
    ) -> None:
        """
        Validate the new fields of an asset or liability account, like add_account, and
        write them over its entry in the accounts YAML, or in the credit cards YAML for a
        credit card account; its name, type and currency stay, and so do the other keys of
        the entry.
        Investment and debt accounts have no initial amount (theirs is that of their
        investments or creditors), and credit card accounts are always liquid. Rolls back
        if the files no longer load.
        """
        snapshot = self.load()
        account = snapshot.accounts.get(name)
        if account is None:
            raise ValueError(f"Unknown account: {name!r}")
        if account.acc_type not in {AccountType.ASSET, AccountType.LIABILITY}:
            raise ValueError(f"Only asset and liability accounts can be edited, not {name!r}")
        fixed_amount = isinstance(account, (InvestmentAccount, DebtAccount))
        fields = _account_fields(
            snapshot, description, 0 if fixed_amount else initial_amount, account.currency.name, is_liquid,
            initial_date, checks,
        )
        if fixed_amount:
            del fields["initial_amount"]
        if isinstance(account, CreditCardAccount):
            del fields["is_liquid"]
            path, section = self.settings.credit_cards_path, ()
        else:
            path = self.settings.accounts_path
            section = (*((INTERNAL,) if account.internal else ()), account.acc_type.value)

        def update(entry: dict) -> dict:
            entry = {k: v for k, v in entry.items() if k not in {"initial_date", "checks"}}
            return {**entry, **fields}

        before = path.read_bytes().decode("utf-8")
        _atomic_write(path, _replace_yaml_entry(before, (*section, name), update))
        try:
            self.load()
        except LedgerDataError as e:
            _atomic_write(path, before)
            raise ValueError(f"Could not edit {name!r}: {e}") from None

    def correct_balance(self, name: str, balance: str | float, check_date: str = "") -> tuple[str, float] | None:
        """
        Correct the balance of an asset or liability account (but an investment one) to
        `balance`, checked by hand at the end of `check_date` (today by default), in its
        currency: a CORRECTION_GAIN or CORRECTION_LOSS movement on that date of the
        difference with its balance then, out of or into equity, in the ledger CSV. The difference in another currency is at
        the value of that currency on that date. Returns the base movement and the amount
        in the base currency, or None if the balance was already right.
        """
        snapshot = self.load()
        account = snapshot.accounts.get(name)
        if account is None or account.acc_type not in {AccountType.ASSET, AccountType.LIABILITY}:
            raise ValueError(f"Only the balances of asset and liability accounts are corrected, not {name!r}")
        if isinstance(account, InvestmentAccount):
            raise ValueError(f"The balance of the investment account {name!r} comes from its investments")  # noqa: TRY004 (a bad pick)
        if not str(balance).strip():
            raise ValueError("The checked balance is required")
        try:
            checked = float(str(balance).replace(",", "").strip())
        except ValueError:
            raise ValueError(f"Invalid checked balance: {balance!r}") from None
        if not math.isfinite(checked):
            raise ValueError(f"Invalid checked balance: {balance!r}")
        check_date = check_date.strip() or dt.date.today().isoformat()
        try:
            dt.date.fromisoformat(check_date)
        except ValueError:
            raise ValueError(f"Invalid check date: {check_date!r}") from None

        if account.currency.is_base_currency:
            rows = snapshot.all_rows
            difference = checked - snapshot.ledger(rows[rows.move_date <= check_date]).balances().get(name, 0.0)
        else:
            value = _value_on(account.currency, check_date, f"to correct {name!r} with")
            difference = (checked - snapshot.own_balance(name, check_date)) * value
        amount = round(abs(difference), 2)
        if amount < 0.005:
            return None
        gain = (difference > 0) == (account.acc_type == AccountType.ASSET)
        base_name = CORRECTION_GAIN if gain else CORRECTION_LOSS
        if base_name not in snapshot.correction_base_movements():
            raise ValueError(
                f"Correcting balances needs {base_name!r}, an internal {'gain' if gain else 'loss'} of the "
                "base movements YAML"
            )
        base = snapshot.base_movements[base_name]
        debit, credit = (name, base.acc_credit.name) if gain else (base.acc_debit.name, name)
        comment = f"Checked balance: {checked:,.2f} {account.currency.name}"
        self.add(check_date, base_name, debit, credit, amount, comment, correction=True)
        return base_name, amount

    def add_result_type(self, name: str, long_name: str, description: str) -> None:
        """Validate a new result type and add it, with the next id, to the base movements YAML."""
        snapshot = self.load()
        existing = {rt.name: rt for rt in snapshot.result_types.values()}
        name = _check_name(name, existing, "result type")
        # The name is a YAML key that later base movements are inserted under.
        if not yaml.safe_dump({name: 0}).startswith(f"{name}:"):
            raise ValueError(f"The result type name {name!r} needs quoting in YAML; use a simpler name")
        long_name = long_name.strip()
        if not long_name:
            raise ValueError("The result type long name is required")
        description = _check_description(description, "result type")
        fields = {
            "id": max(snapshot.result_types, default=0) + 1,
            "long_name": long_name,
            "description": description,
        }
        self._add_yaml_entry(self.settings.base_movements_path, ("result_types",), name, fields)

    def add_base_movement(
        self,
        name: str,
        move_type: str,
        description: str,
        debit: str = "",
        credit: str = "",
        result_type: str = "",
    ) -> None:
        """
        Validate a new base movement and add it to the base movements YAML: gains
        and losses under their result type, transfers in the `transfer` section.
        A side set to one of the RESERVED names takes any such account (see SPECIAL_SIDES).
        """
        snapshot = self.load()
        name = _check_name(name, snapshot.base_movements, "base movement")
        try:
            move_type = MovementType(move_type)
        except ValueError:
            raise ValueError(f"Invalid movement type: {move_type!r}") from None
        description = _check_description(description, "base movement")
        if move_type == MovementType.TRANSFER:
            section = (MovementType.TRANSFER.value,)
        else:
            result_types = {rt.name: rt for rt in snapshot.result_types.values()}
            if not result_type:
                raise ValueError("Gains and losses require a result type")
            if result_type not in result_types:
                raise ValueError(f"Unknown result type: {result_type!r}")
            if result_types[result_type].internal:
                raise ValueError(f"Result type {result_type!r} is internal and cannot get new base movements")
            section = ("result_types", result_type, "base_movements", move_type.value)
        # Gains always credit equity and losses always debit it.
        if move_type == MovementType.GAIN:
            credit = ""
        elif move_type == MovementType.LOSS:
            debit = ""
        free = {a.name for a in snapshot.free_accounts()}
        for acc in (debit, credit):
            if acc and acc not in SPECIAL_SIDES and acc not in free:
                raise ValueError(f"Unknown or non-selectable account: {acc!r}")
        if debit and debit not in SPECIAL_SIDES and debit == credit:
            raise ValueError("Debit and credit accounts must be different")

        fields = {"description": description}
        if debit:
            fields["debit"] = debit
        if credit:
            fields["credit"] = credit
        self._add_yaml_entry(self.settings.base_movements_path, section, name, fields)

    def _add_yaml_entry(
        self, path: Path, section: tuple[str, ...], name: str, fields: dict
    ) -> None:
        """
        Insert `name: fields` at the end of a (nested) section, as text, so the
        rest of the file (comments, extra keys, ordering) is left as is. Rolls
        back if the files no longer load.
        """
        before = path.read_bytes().decode("utf-8")
        _atomic_write(path, _insert_yaml_entry(before, section, name, fields))
        try:
            self.load()
        except LedgerDataError as e:
            _atomic_write(path, before)
            raise ValueError(f"Could not add {name!r}: {e}") from None

    def load_cpi(self) -> Series | None:
        """The daily CPI of the base currency, or None if the currencies can't be read."""
        try:
            base_currency, _ = Currency.load_yaml(str(self.settings.currencies_path))
        except (OSError, KeyError, ValueError, AssertionError):
            return None
        return base_currency.customer_price_index

    def load_work(self) -> Work:
        """
        The work history: one row per month, in order and with no missing months
        (a month without a job is a row with no company, position or salary).
        """
        path = self.settings.work_path
        try:
            if _existing(path) and path.read_bytes().strip():
                rows = read_csv(path, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
                missing = [c for c in WORK_COLUMNS if c != PAYMENT_DATE and c not in rows.columns]
                if missing:
                    raise ValueError(f"The work CSV is missing the columns {missing}")
            else:
                rows = DataFrame(columns=WORK_COLUMNS, dtype=str)
            deductions = deduction_columns(rows.columns)
            months = [_work_month(row, deductions) for _, row in rows.iterrows()]
            WorkMonth.check_sequence(months)
        except (OSError, KeyError, ValueError) as e:
            raise LedgerDataError(f"{type(e).__name__}: {e}") from e
        return Work(months, rows)

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

    def delete(
        self, index: int, fingerprint: str | None = None, debt: bool = False, card_payment: bool = False,
        correction: bool = False,
    ) -> None:
        """
        Delete a row of the ledger CSV by position, optionally checking it is the
        expected one. Never of an internal base movement, but a debt movement with
        `debt` (from the Debt tab), a card payment with `card_payment` (from the
        Credit Cards tab) or a balance correction with `correction`, which are only
        deleted that way (see _check_tab).
        """
        self._delete_row("rows", index, fingerprint, debt, card_payment, correction)

    def edit(
        self,
        index: int,
        fingerprint: str | None,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount: str | float,
        comment: str = "",
        debt: bool = False,
        card_payment: bool = False,
    ) -> None:
        """
        Replace a row of the ledger CSV by position with a movement of the same tab,
        optionally checking it is the expected one, like delete: of the Ledger tab, else
        of the Debt tab with `debt` or of the Credit Cards tab with `card_payment` (see
        _check_tab). The row keeps its id and any other column.
        """
        snapshot = self.load()
        current = self.current_row(snapshot, "rows", index, fingerprint)
        _check_invest(snapshot, current["base_movement"], False, "edited")
        _check_tab(snapshot, current["base_movement"], False, debt, card_payment, "edited")
        _, new_row = _movement_row(
            snapshot, move_date, base_movement, acc_debit, acc_credit, amount, comment,
            False, "", "", debt, card_payment,
        )
        _fit_row(new_row, snapshot.rows.columns, "ledger")  # every value has a column
        replace(snapshot, rows=_with_row(snapshot.rows, index, new_row)).ledger().check_accounts()
        self._replace_line(snapshot, "rows", index, new_row)

    def edit_investment(
        self,
        source: str,
        index: int,
        fingerprint: str | None,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount: str | float,
        comment: str = "",
        investment: str = "",
        quantity: str | float = "",
    ) -> None:
        """
        Replace a row of the investments CSV, or of the interest CSV with `source`
        "interest", by position with a movement of the Investments tab, checked like
        add. It stays in its CSV, and keeps its id and any other column.
        """
        snapshot = self.load()
        self.current_row(snapshot, source, index, fingerprint)
        field, new_row = _movement_row(
            snapshot, move_date, base_movement, acc_debit, acc_credit, amount, comment,
            True, investment, quantity, False, False,
        )
        if field != source:
            where = {"interest": "interest on money", "investments": "investments"}
            raise ValueError(
                f"A movement of the {where[source]} can't become one of the {where[field]}: "
                "delete it and add it again instead"
            )
        frame = getattr(snapshot, field)
        _fit_row(new_row, frame.columns, field)  # every value has a column
        changed = replace(snapshot, **{field: _with_row(frame, index, new_row)})
        changed.ledger().check_accounts()
        changed.investment_accounts()  # e.g. not selling more than is held
        if field == "investments":
            _check_known_investment(snapshot, acc_debit, acc_credit, new_row[INVESTMENT_COLUMN])
        self._replace_line(snapshot, field, index, new_row)

    def current_row(self, snapshot: Snapshot, field: str, index: int, fingerprint: str | None):
        """The row of the CSV of a Snapshot `field` at this position, if it is the expected one."""
        rows = getattr(snapshot, field)
        what, _, fingerprint_of = self._csv_of(field)
        if not 0 <= index < len(rows):
            raise StaleRowError(f"Row {index} of the {what} does not exist")
        if fingerprint is not None and fingerprint_of(rows.iloc[index]) != fingerprint:
            raise StaleRowError(f"The {what} changed since this page was loaded; reload and try again")
        return rows.iloc[index]

    def _csv_of(self, field: str) -> tuple[str, Path, Callable]:
        """What the CSV of a Snapshot `field` holds, its path and how its rows are fingerprinted."""
        s = self.settings
        return {
            "rows": ("ledger", s.ledger_path, row_fingerprint),
            "investments": ("investments", s.investment_movements_path, row_fingerprint),
            "interest": ("interest on money", s.interest_path, interest_fingerprint),
            "conversions": ("conversions", s.conversions_path, conversion_fingerprint),
        }[field]

    def _replace_line(self, snapshot: Snapshot, field: str, index: int, new_row: dict) -> None:
        """
        Replace the values of `new_row` in the line of a row of the CSV of a Snapshot
        `field`, keeping its id and other columns.
        """
        csv_file = self._read_file(len(getattr(snapshot, field)), self._csv_of(field)[1])
        line = csv_file.row_lines[index]
        header = next(csv.reader([csv_file.lines[0]], delimiter=";"))
        fields = next(csv.reader([csv_file.lines[line]], delimiter=";"))
        fields += [""] * (len(header) - len(fields))
        for col, value in new_row.items():
            if col in header and col != ID_COLUMN:
                fields[header.index(col)] = value
        buf = io.StringIO()
        csv.writer(buf, delimiter=";", lineterminator="").writerow(fields)
        csv_file.lines[line] = buf.getvalue()
        self._write_file(csv_file)

    def delete_investment(self, index: int, fingerprint: str | None = None) -> None:
        """Delete a row of the investments CSV by position, like delete."""
        self._delete_row("investments", index, fingerprint)

    def set_service_value(self, month: str, column: str, amount: str | float = "") -> None:
        """Set the value of a column of the services CSV in a month, keeping the others (see save_service_month)."""
        snapshot = self.load()
        month = parse_month(month, "month")
        values = dict(service_months(snapshot)).get(month, {})
        self.save_service_month(month, {**{c: f"{v:,.2f}" for c, v in values.items()}, column.strip(): amount})

    def save_service_month(self, month: str, values: dict[str, str | float]) -> None:
        """
        Save the row of a month of the services CSV (see _service_rows), replacing it, with
        the value of each column of a service (an empty one is none); the columns left out
        are emptied. A month not in the CSV yet gets its row, in month order, and a column
        not in it yet with a value gets its column. The CSV is created if missing.
        """
        snapshot = self.load()
        path = self.settings.services_path
        if path is None:
            raise ValueError("No services CSV is set")
        month = parse_month(month, "month")
        known = service_columns(snapshot)
        values = {c.strip(): str(v).strip() for c, v in values.items()}
        unknown = [c for c in values if c not in known]
        if unknown:
            raise ValueError(f"{unknown[0]!r} is not a column of a service of {DEBT_ACCOUNT} in the creditors YAML")
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

    def delete_interest(self, index: int, fingerprint: str | None = None) -> None:
        """Delete a row of the interest CSV by position, like delete."""
        self._delete_row("interest", index, fingerprint)

    def _delete_row(
        self, field: str, index: int, fingerprint: str | None, debt: bool = False, card_payment: bool = False,
        correction: bool = False,
    ) -> None:
        """
        Delete a row by position from the CSV of a Snapshot `field` ("rows",
        "investments", "interest" or "services"), optionally checking it is the expected one.
        """
        snapshot = self.load()
        rows = getattr(snapshot, field)
        path = self._csv_of(field)[1]
        self.current_row(snapshot, field, index, fingerprint)
        if field == "rows":
            _check_invest(snapshot, rows.iloc[index]["base_movement"], False, "deleted")
            _check_tab(snapshot, rows.iloc[index]["base_movement"], False, debt, card_payment, "deleted", correction)
        changed = replace(snapshot, **{field: rows.drop(index=rows.index[index])})
        changed.ledger().check_accounts()
        changed.investment_accounts()  # e.g. not deleting a purchase of something sold later
        csv_file = self._read_file(len(rows), path)
        del csv_file.lines[csv_file.row_lines[index]]
        self._write_file(csv_file)

    def load_conversions(self) -> DataFrame:
        """The conversions CSV rows, all columns as strings; none if it is missing."""
        return _load_movements(self.settings.conversions_path, CONVERSION_COLUMNS, "conversions")

    def add_conversion(
        self,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount_base: str | float,
        amount_other: str | float,
        comment: str = "",
    ) -> None:
        """
        Validate a new conversion (see _check_conversion) and append it to the
        conversions CSV, creating it if missing, with the next id of the CSV (see _appended).
        """
        if self.settings.conversions_path is None:
            raise ValueError("No conversions CSV is set")
        snapshot = self.load()
        new_row = _conversion_row(snapshot, move_date, base_movement, acc_debit, acc_credit, amount_base, amount_other, comment)
        self._add_conversions(snapshot, [new_row])

    def _add_conversions(self, snapshot: Snapshot, new_rows: list[dict]) -> None:
        """Append these validated conversions to the conversions CSV, all or none, if the ledger still holds."""
        path = self.settings.conversions_path
        if path is None:
            raise ValueError("No conversions CSV is set")
        conversions = concat([snapshot.conversions, DataFrame(new_rows)], ignore_index=True)
        try:
            replace(snapshot, conversions=conversions).ledger().check_accounts()
        except AssertionError as e:
            raise ValueError(str(e)) from None
        self._write_file(self._appended(path, CONVERSION_ID, new_rows, CONVERSION_COLUMNS))

    def edit_conversion(
        self,
        index: int,
        fingerprint: str | None,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount_base: str | float,
        amount_other: str | float,
        comment: str = "",
    ) -> None:
        """
        Replace a row of the conversions CSV by position with a conversion checked like
        add_conversion, optionally checking it is the expected one. It keeps its id.
        """
        snapshot = self.load()
        self.current_row(snapshot, "conversions", index, fingerprint)
        new_row = _conversion_row(snapshot, move_date, base_movement, acc_debit, acc_credit, amount_base, amount_other, comment)
        new_row = {col: v for col, v in new_row.items() if col in CONVERSION_COLUMNS}  # keep the other columns
        conversions = _with_row(snapshot.conversions, index, new_row)
        try:
            replace(snapshot, conversions=conversions).ledger().check_accounts()
        except AssertionError as e:
            raise ValueError(str(e)) from None
        self._replace_line(snapshot, "conversions", index, new_row)

    def add_exchange(
        self,
        move_date: str,
        sold: str,
        bought: str,
        acc_from: str,
        acc_to: str,
        amount_sold: str | float,
        amount_bought: str | float,
        comment: str = "",
    ) -> str:
        """
        Add a conversion selling one currency for another: from an account in the `sold`
        currency to one in the `bought` currency. With the base one, its base movement is
        picked from the currencies (see exchange_base_movement), and returned. Between two
        other currencies, it is a cross conversion through the INTERMEDIARY account (see
        _cross_rows), and CROSS_CONVERSION is returned.
        """
        snapshot = self.load()
        if is_cross(snapshot, sold, bought):
            self._add_conversions(snapshot, _cross_rows(
                snapshot, move_date, sold, bought, acc_from, acc_to, amount_sold, amount_bought, comment,
            ))
            return CROSS_CONVERSION
        base_movement, *args = _exchange_args(snapshot, sold, bought, acc_from, acc_to, amount_sold, amount_bought)
        self.add_conversion(move_date, base_movement, *args, comment)
        return base_movement

    def edit_exchange(
        self,
        index: int,
        fingerprint: str | None,
        move_date: str,
        sold: str,
        bought: str,
        acc_from: str,
        acc_to: str,
        amount_sold: str | float,
        amount_bought: str | float,
        comment: str = "",
    ) -> str:
        """
        Replace a row of the conversions CSV with a conversion like add_exchange, returning
        its base movement. Both rows of a cross conversion (see conversion_pairs) are
        replaced, with another cross conversion: a conversion doesn't change its kind.
        """
        snapshot = self.load()
        pairs = conversion_pairs(snapshot)
        cross = is_cross(snapshot, sold, bought)
        if cross != (index in pairs):
            kind = "between two other currencies" if index in pairs else f"with {snapshot.base_currency.name}"
            raise ValueError(f"This conversion is {kind} and stays one: delete it and add the new one instead")
        if not cross:
            base_movement, *args = _exchange_args(snapshot, sold, bought, acc_from, acc_to, amount_sold, amount_bought)
            self.edit_conversion(index, fingerprint, move_date, base_movement, *args, comment)
            return base_movement
        self.current_row(snapshot, "conversions", index, fingerprint)
        first = min(index, pairs[index])
        new_rows = _cross_rows(
            snapshot, move_date, sold, bought, acc_from, acc_to, amount_sold, amount_bought, comment,
        )
        new_rows = [{col: v for col, v in row.items() if col in CONVERSION_COLUMNS} for row in new_rows]  # keep the other columns
        conversions = _with_row(_with_row(snapshot.conversions, first, new_rows[0]), first + 1, new_rows[1])
        try:
            replace(snapshot, conversions=conversions).ledger().check_accounts()
        except AssertionError as e:
            raise ValueError(str(e)) from None
        self._replace_line(snapshot, "conversions", first, new_rows[0])
        self._replace_line(snapshot, "conversions", first + 1, new_rows[1])
        return CROSS_CONVERSION

    def add_debt_movement(
        self,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount: str | float,
        who: str,
        comment: str = "",
    ) -> None:
        """
        Add a movement of a debt base movement to the ledger CSV, about `who`, someone of
        its debt account but a company (their debt comes from the work history): its
        comment names them first (prefixed if it doesn't), so the Debt tab adds it to them.
        """
        comment = _debt_comment(self.load(), acc_debit, acc_credit, who, comment)
        self.add(move_date, base_movement, acc_debit, acc_credit, amount, comment, debt=True)

    def edit_debt_movement(
        self,
        index: int,
        fingerprint: str | None,
        move_date: str,
        base_movement: str,
        acc_debit: str,
        acc_credit: str,
        amount: str | float,
        who: str,
        comment: str = "",
    ) -> None:
        """Replace a debt movement of the ledger CSV by position, about `who` like add_debt_movement."""
        comment = _debt_comment(self.load(), acc_debit, acc_credit, who, comment)
        self.edit(index, fingerprint, move_date, base_movement, acc_debit, acc_credit, amount, comment, debt=True)

    def delete_conversion(self, index: int, fingerprint: str | None = None) -> None:
        """
        Delete a row of the conversions CSV by position, optionally checking it is the
        expected one: both rows of a cross conversion (see conversion_pairs).
        """
        snapshot = self.load()
        rows = snapshot.conversions
        if not 0 <= index < len(rows):
            raise StaleRowError(f"Conversion {index} does not exist")
        if fingerprint is not None and conversion_fingerprint(rows.iloc[index]) != fingerprint:
            raise StaleRowError("The conversions changed since this page was loaded; reload and try again")
        indexes = sorted({index, conversion_pairs(snapshot).get(index, index)})
        replace(snapshot, conversions=rows.drop(index=rows.index[indexes])).ledger().check_accounts()
        csv_file = self._read_file(len(rows), self.settings.conversions_path)
        for i in reversed(indexes):
            del csv_file.lines[csv_file.row_lines[i]]
        self._write_file(csv_file)

    def _appended(self, path: Path, prefix: str, row: dict | list[dict], header: list[str]) -> _CsvFile:
        """
        A CSV of movements with its ids frozen (see freeze_ids) and this row (or these
        rows, in order) appended with the next ids, ready to write. A missing or empty
        CSV is a new one with this header and the ID_COLUMN.
        """
        if _existing(path) and path.read_bytes().strip():
            self._freeze_file(path, prefix)
            rows = read_csv(path, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
            columns = list(rows.columns)
        else:
            columns = [c for c in header if c != ID_COLUMN] + [ID_COLUMN]
            rows = DataFrame(columns=columns, dtype=str)
        number = int(next_id(rows, prefix).split("-")[1])
        csv_file = self._read_file(len(rows), path, header=columns)
        for i, new_row in enumerate([row] if isinstance(row, dict) else row):
            values = new_row | {ID_COLUMN: f"{prefix}-{number + i}"}
            buf = io.StringIO()
            csv.writer(buf, delimiter=";", lineterminator="").writerow([values.get(c, "") for c in columns])
            csv_file.lines.append(buf.getvalue())
        return csv_file

    def _read_file(self, n_rows: int, path: Path, header: list[str] | None = None) -> _CsvFile:
        """
        Split a CSV into raw lines, so edits touch only the affected line and
        every other byte (quoting, BOM, line endings) is kept as is. With a `header`,
        a missing or empty CSV is a new one with it.
        """
        raw = path.read_bytes().decode("utf-8") if path.exists() else ""
        if header is not None and not raw.strip():
            raw = ";".join(header) + "\n"
        bom = raw.startswith(BOM)
        text = raw.removeprefix(BOM)
        newline = "\r\n" if "\r\n" in text else "\n"
        trailing = text.endswith(newline)
        lines = text.removesuffix(newline).split(newline) if text else []
        # Line 0 is the header; blank lines are skipped by the CSV reader.
        row_lines = [i for i, line in enumerate(lines) if i > 0 and line.strip()]
        if len(row_lines) != n_rows:
            raise LedgerDataError(
                f"The CSV {path} has multi-line fields; edit it outside pledger"
            )
        return _CsvFile(path, lines, row_lines, newline, bom, trailing)

    def _write_file(self, csv_file: _CsvFile) -> None:
        """Atomically replace a CSV."""
        text = csv_file.newline.join(csv_file.lines)
        if csv_file.trailing:
            text += csv_file.newline
        if csv_file.bom:
            text = BOM + text
        _atomic_write(csv_file.path, text)
