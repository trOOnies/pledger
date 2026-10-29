"""account_store.py: the accounts, result types and base movements of the YAMLs, and the balance corrections."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from pathlib import Path

import yaml

from pledger.enums import AccountType, MovementType
from pledger.models import (
    INTERNAL,
    SPECIAL_SIDES,
    CreditCardAccount,
    DebtAccount,
    InvestmentAccount,
)
from pledger.web.store.common import (
    LedgerDataError,
    _atomic_write,
    _check_description,
    _check_name,
    _value_on,
)
from pledger.web.store.constants import CORRECTION_GAIN, CORRECTION_LOSS
from pledger.web.store.movement_store import MovementStore
from pledger.web.store.yaml_edit import (
    _account_fields,
    _insert_yaml_entry,
    _replace_yaml_entry,
)


class AccountStore(MovementStore):
    """Adds and edits the accounts, result types and base movements, and corrects the balances."""

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
