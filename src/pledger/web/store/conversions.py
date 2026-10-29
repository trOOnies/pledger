"""conversions.py: the conversions between currencies, and the results of the accounts in other currencies."""

from __future__ import annotations

import datetime as dt
import math
from typing import TYPE_CHECKING

from pandas import DataFrame, Timestamp

from pledger.enums import AccountType
from pledger.models import FOREIGN_CURRENCY, INTERMEDIARY, Account, InvestmentAccount
from pledger.web.store.common import (
    _amount_of,
    _concat,
    _generated_rows,
    _parse_amount,
    _required_base,
    _value_on,
    own_initial_amount,
)
from pledger.web.store.constants import (
    BASE_AMOUNT,
    CONVERSION_BOUGHT,
    CONVERSION_SOLD,
    CONVERSIONS,
    ID_COLUMN,
    INITIAL_ID,
    OTHER_AMOUNT,
    RESULT_GAIN,
    RESULT_ID,
    RESULT_LOSS,
)

if TYPE_CHECKING:
    from pledger.web.store.snapshot import Snapshot


def _conversion_row(
    snapshot: Snapshot,
    move_date: str,
    base_movement: str,
    acc_debit: str,
    acc_credit: str,
    amount_base: str | float,
    amount_other: str | float,
    comment: str,
) -> dict:
    """A conversions CSV row, validated (see _check_conversion) but against the balances."""
    new_row = {col: "" for col in snapshot.conversions.columns} | {
        "move_date": move_date,
        "base_movement": base_movement,
        "acc_debit": acc_debit,
        "acc_credit": acc_credit,
        BASE_AMOUNT: str(amount_base).strip(),
        OTHER_AMOUNT: str(amount_other).strip(),
    }
    if acc_debit == acc_credit:
        raise ValueError("Debit and credit accounts must be different")
    if base_movement not in snapshot.conversion_base_movements():
        names = sorted(snapshot.conversion_base_movements())
        raise ValueError(
            f"Conversions must be of {' or '.join(map(repr, names)) or 'a conversion base movement'}, "
            f"not {base_movement!r}"
        )
    other, base_value, other_value = _check_conversion(snapshot, new_row)
    expected = CONVERSION_BOUGHT if other.name == acc_debit else CONVERSION_SOLD
    if base_movement != expected:
        raise ValueError(f"Moving {other.currency.name} {'into' if other.name == acc_debit else 'out of'} "
                         f"{other.name} is a {expected!r} conversion, not a {base_movement!r} one")
    if base_value == 0:
        raise ValueError("The amounts of a conversion must be positive")
    new_row[BASE_AMOUNT], new_row[OTHER_AMOUNT] = f"{base_value:,.2f}", f"{other_value:,.4f}"
    if "comment" in new_row:
        new_row["comment"] = comment.replace(";", ",").strip()
    elif comment.strip():
        raise ValueError("The conversions CSV has no 'comment' column")
    return new_row


def _exchange_args(
    snapshot: Snapshot, sold: str, bought: str, acc_from: str, acc_to: str,
    amount_sold: str | float, amount_bought: str | float,
) -> tuple[str, str, str, str | float, str | float]:
    """
    The base movement, debit and credit accounts and amounts in the base and the other
    currency of a conversion selling one currency for another (see LedgerStore.add_exchange).
    """
    base_movement = exchange_base_movement(snapshot, sold, bought)
    for name, currency, side in ((acc_from, sold, "from"), (acc_to, bought, "to")):
        account = snapshot.accounts.get(name)
        if account is None:
            raise ValueError(f"Unknown account: {name!r}")
        if account.currency.name != currency:
            raise ValueError(f"The account {side} must be in {currency}, but {name} is in {account.currency.name}")
    amounts = (amount_sold, amount_bought) if sold == snapshot.base_currency.name else (amount_bought, amount_sold)
    return base_movement, acc_to, acc_from, *amounts


def is_cross(snapshot: Snapshot, sold: str, bought: str) -> bool:
    """Whether a conversion of these currencies is a cross one: between two different other currencies."""
    others = set(snapshot.currencies) - {snapshot.base_currency.name}
    return sold in others and bought in others and sold != bought


def _cross_rows(
    snapshot: Snapshot, move_date: str, sold: str, bought: str, acc_from: str, acc_to: str,
    amount_sold: str | float, amount_bought: str | float, comment: str,
) -> list[dict]:
    """
    The two conversions CSV rows of a cross conversion (see is_cross), validated but
    against the balances: a CONVERSION_SOLD of `amount_sold` out of `acc_from` into the
    INTERMEDIARY account, and a CONVERSION_BOUGHT of `amount_bought` out of it into
    `acc_to`, both of the amount sold at the value of its currency on that date, so the
    INTERMEDIARY is left as it was.
    """
    through = INTERMEDIARY
    for name, currency, side in ((acc_from, sold, "from"), (acc_to, bought, "to")):
        account = snapshot.accounts.get(name)
        if account is None:
            raise ValueError(f"Unknown account: {name!r}")
        if account.currency.name != currency:
            raise ValueError(f"The account {side} must be in {currency}, but {name} is in {account.currency.name}")
    try:
        dt.date.fromisoformat(move_date)
    except ValueError:
        raise ValueError(f"Invalid date: {move_date!r}") from None
    value = _parse_amount(amount_sold, f"amount sold in {sold}")
    base_value = round(value * _value_on(snapshot.currencies[sold], move_date, f"to convert {sold} to {bought}"), 2)
    return [
        _conversion_row(snapshot, move_date, CONVERSION_SOLD, through, acc_from, base_value, amount_sold, comment),
        _conversion_row(snapshot, move_date, CONVERSION_BOUGHT, acc_to, through, base_value, amount_bought, comment),
    ]


def conversion_pairs(snapshot: Snapshot) -> dict[int, int]:
    """
    The cross conversions of the conversions CSV (see _cross_rows): the row of each of
    their two rows to that of the other. A CONVERSION_SOLD row debiting the INTERMEDIARY
    right followed by a CONVERSION_BOUGHT one crediting it, on the same date and of the
    same amount in the base currency, of two other currencies.
    """
    rows, pairs = snapshot.conversions, {}
    for i in range(len(rows) - 1):
        sold, bought = rows.iloc[i], rows.iloc[i + 1]
        if (
            i not in pairs and sold["base_movement"] == CONVERSION_SOLD and bought["base_movement"] == CONVERSION_BOUGHT
            and sold["move_date"] == bought["move_date"] and sold["acc_debit"] == bought["acc_credit"] == INTERMEDIARY
            and _amount_of(sold[BASE_AMOUNT]) == _amount_of(bought[BASE_AMOUNT])
            and is_cross(
                snapshot, snapshot.accounts[sold["acc_credit"]].currency.name,
                snapshot.accounts[bought["acc_debit"]].currency.name,
            )
        ):
            pairs[i], pairs[i + 1] = i + 1, i
    return pairs


def _conversion_rows(snapshot: Snapshot, start: int) -> DataFrame:
    """
    The movements of the conversions CSV, as CSV rows indexed from `start` down, with
    the amount in the base currency (see _check_conversion).
    """
    new_rows = []
    for _, row in snapshot.conversions.iterrows():
        _check_conversion(snapshot, row)
        new_rows.append({
            **{col: row[col] for col in snapshot.rows.columns if col in row.index},
            "amount": row[BASE_AMOUNT],
        })
    return _generated_rows(new_rows, snapshot.rows.columns, start)


def exchange_base_movement(snapshot: Snapshot, sold: str, bought: str) -> str:
    """
    The base movement of a conversion selling the `sold` currency for the `bought` one:
    CONVERSION_BOUGHT if the one that isn't the base currency is bought, else
    CONVERSION_SOLD. One of them must be the base currency, and the base movement a
    conversion one.
    """
    for currency in (sold, bought):
        if currency not in snapshot.currencies:
            raise ValueError(f"Unknown currency: {currency!r}")
    base = snapshot.base_currency.name
    if (sold == base) == (bought == base):
        raise ValueError(
            f"Conversions sell or buy {base} for another currency, or one other currency for another, "
            f"not {sold} for {bought}"
        )
    name = CONVERSION_BOUGHT if sold == base else CONVERSION_SOLD
    if name not in snapshot.conversion_base_movements():
        raise ValueError(f"No {name!r} base movement: add it as an internal transfer of the base movements YAML")
    return name


def _check_conversion(snapshot: Snapshot, row) -> tuple[Account, float, float]:
    """
    A conversions CSV row must be of a conversion base movement (see
    Snapshot.conversion_base_movements), between
    an account in the base currency and one in another currency, with the amount in
    each. Returns the one in the other currency, with both amounts.
    """
    date = row["move_date"]
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        raise ValueError(f"Invalid date of a conversion: {date!r}") from None
    base = snapshot.base_movements.get(row["base_movement"])
    if base is None:
        raise ValueError(f"Unknown base movement of a conversion: {row['base_movement']!r} ({date})")
    if base.name not in snapshot.conversion_base_movements():
        raise ValueError(
            f"Conversions must be of {' or '.join(map(repr, sorted(CONVERSIONS)))} (internal transfers), "
            f"not {base.name!r} ({date})"
        )
    names = (row["acc_debit"], row["acc_credit"])
    unknown = [n for n in names if n not in snapshot.accounts]
    if unknown:
        raise ValueError(f"Unknown account of a conversion: {unknown[0]!r} ({date})")
    others = [snapshot.accounts[n] for n in names if not snapshot.accounts[n].currency.is_base_currency]
    if len(others) != 1:
        raise ValueError(
            f"Conversions must be between an account in {snapshot.base_currency.name} and one in "
            f"another currency, not {names[0]} and {names[1]} ({date})"
        )
    amounts = (
        _parse_amount(row[BASE_AMOUNT], f"amount in {snapshot.base_currency.name} of a conversion ({date})"),
        _parse_amount(row[OTHER_AMOUNT], f"amount in {others[0].currency.name} of a conversion ({date})"),
    )
    if (amounts[0] == 0) != (amounts[1] == 0):
        raise ValueError(f"The amounts of a conversion must both be zero or both positive ({date})")
    return others[0], *amounts


def _result_rows(snapshot: Snapshot, start: int) -> tuple[DataFrame, dict[str, float], dict[str, list[tuple]]]:
    """
    The results of the accounts in other currencies, as CSV rows indexed from `start`
    down, their balances in their currency and their movements: (date, amount in the
    base currency, in their own or None, and the row of the conversions CSV of a
    conversion or None). Investment accounts are left out:
    their value changes with their capital gains and losses.

    The balance of each one in its currency follows its movements: the conversions
    with their amount in it, and the others (its initial amount too) at the value of
    the currency on their date (the last one up to then, else its first one). At the
    end of each date of the currencies CSV from its first movement on, it is
    revalued: the result is a RESULT_GAIN or RESULT_LOSS movement against the equity
    account, of the difference between that balance at that value of the currency
    and its balance in the ledger, to the cent. Never in the initial week, which only
    has the initial movements: those dates are revalued on the next one after it.
    """
    accounts = {
        n: a for n, a in snapshot.accounts.items()
        if not a.currency.is_base_currency and a.acc_type != AccountType.EQUITY
        and not isinstance(a, InvestmentAccount)
    }
    # Account name to its movements: (date, amount in the base currency, in its own or None,
    # row of the conversions CSV or None).
    moves: dict[str, list[tuple[str, float, float | None, int | None]]] = {n: [] for n in accounts}

    def add(date: str, debit: str, credit: str, amount: float, other: float | None, row: int | None = None) -> None:
        for name, sign in ((debit, 1), (credit, -1)):
            if name in accounts:
                sign = sign if accounts[name].acc_type == AccountType.ASSET else -sign
                moves[name].append((date, sign * amount, None if other is None else sign * other, row))

    # The initial amounts in their currency, exactly (see _initial_rows)
    for _, row in snapshot.initial_rows.iterrows():
        acc = snapshot.accounts[row[ID_COLUMN].removeprefix(f"{INITIAL_ID}-")]
        other = abs(acc.initial_amount) if own_initial_amount(acc) else None
        add(row["move_date"], row["acc_debit"], row["acc_credit"], float(row["amount"]), other)
    rows = _concat(
        [snapshot.rows, snapshot.investment_rows, snapshot.interest_rows, snapshot.salary_rows],
        snapshot.rows.columns,
    )
    for date, debit, credit, amount in zip(rows["move_date"], rows["acc_debit"], rows["acc_credit"], rows["amount"]):
        add(date, debit, credit, float(amount.replace(",", "").replace(" ", "") or 0), None)
    for i, (_, row) in enumerate(snapshot.conversions.iterrows()):
        _, amount, other = _check_conversion(snapshot, row)
        add(row["move_date"], row["acc_debit"], row["acc_credit"], amount, other, i)

    new_rows, balances = [], {}
    week = snapshot.initial_week
    last_initial = week.end.isoformat() if week is not None else ""
    for name, acc in accounts.items():
        values = acc.currency.base_currency_mapping
        account_moves = sorted(moves[name], key=lambda m: m[0])  # stable: in their order on each date
        if not account_moves or not len(values):
            balances[name] = 0.0 if not account_moves else math.nan
            continue
        book = other = 0.0  # its balance in the base currency and in its own
        pending = iter(account_moves)
        move = next(pending, None)
        for when, value in values[values.index >= Timestamp(account_moves[0][0])].items():
            day = when.date().isoformat()
            while move is not None and move[0] <= day:
                book, other = book + move[1], other + _in_currency(move, values)
                move = next(pending, None)
            if day <= last_initial:
                continue  # the initial week only has the initial movements: revalued after it
            result = round(other * value - book, 2)
            if abs(result) < 0.005:
                continue
            gain = (result > 0) == (acc.acc_type == AccountType.ASSET)
            base = _required_base(
                snapshot.base_movements, RESULT_GAIN if gain else RESULT_LOSS, gain,
                "to revalue the accounts in other currencies", FOREIGN_CURRENCY,
            )
            sides = (name, base.acc_credit.name) if base.name == RESULT_GAIN else (base.acc_debit.name, name)
            new_rows.append({
                "move_date": day, "base_movement": base.name, "acc_debit": sides[0], "acc_credit": sides[1],
                "amount": f"{abs(result):.2f}",
                ID_COLUMN: f"{RESULT_ID}-{name}-{day}",
                **({"comment": f"{other:,.4f} {acc.currency.name} at {value:,.4f}"}
                   if "comment" in snapshot.rows.columns else {}),
            })
            book += result
        while move is not None:
            other += _in_currency(move, values)
            move = next(pending, None)
        balances[name] = float(other)
    return _generated_rows(new_rows, snapshot.rows.columns, start), balances, moves


def _in_currency(move: tuple, values) -> float:
    """The amount of a movement in the currency of these values (see _result_rows)."""
    date, amount, other = move[:3]
    if other is not None:
        return other
    up_to = values[values.index <= Timestamp(date)]
    return amount / (up_to.iloc[-1] if len(up_to) else values.iloc[0])
