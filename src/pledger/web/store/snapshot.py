"""snapshot.py: everything loaded from the data files for one request."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import TYPE_CHECKING

from pandas import DataFrame

from pledger.enums import AccountType, LedgerSide, MovementType
from pledger.ledger import Ledger
from pledger.models import (
    CREDIT_CARD,
    DEBT_ACCOUNT,
    INVESTMENT,
    SPECIAL_SIDES,
    Account,
    Currency,
    DebtAccount,
    InvestmentAccount,
    MovementBase,
    ResultType,
    initial_week,
)
from pledger.web.store import common
from pledger.web.store.common import _concat, stored_ids
from pledger.web.store.constants import (
    BEFORE_COLUMN,
    CARD_PAYMENT_BASES,
    CONVERSION_ID,
    CONVERSIONS,
    CORRECTIONS,
    DEBT_BASES,
    GENERATED,
    ID_COLUMN,
    INITIAL_GAIN,
    INITIAL_PRICE,
    INTEREST_ID,
    INVEST,
    INVESTMENT_COLUMN,
    INVESTMENT_COLUMNS,
    INVESTMENT_ID,
    LEDGER_ID,
    SERVICE_COLUMN,
)
from pledger.web.store.conversions import _conversion_rows, _in_currency, _result_rows
from pledger.web.store.investments import (
    _apply_investments,
    _capital_rows,
    _interest_rows,
    _investment_rows,
)
from pledger.web.store.movements import _apply_debts, _initial_rows
from pledger.web.store.services import _service_rows
from pledger.web.store.work import _salary_rows
from pledger.weeks import Week

if TYPE_CHECKING:
    from pledger.web.store.work import Work


@dataclass
class Snapshot:
    """Everything loaded from disk for one request."""

    accounts: dict[str, Account]
    base_movements: dict[str, MovementBase]
    result_types: dict[int, ResultType]
    rows: DataFrame  # raw ledger CSV rows, all columns as strings
    currencies: dict[str, Currency]  # the base currency first
    conversions: DataFrame  # raw conversions CSV rows, all columns as strings
    investments: DataFrame  # raw investments CSV rows, all columns as strings
    interest: DataFrame  # raw interest CSV rows, all columns as strings
    prices: DataFrame  # raw prices CSV rows, all columns as strings
    services: DataFrame  # raw services CSV rows, all columns as strings
    work: Work
    # The top-level previous month of the accounts YAML, if any (see Account.load_yaml)
    previous_month: str | None = None
    # The first date of the ledger: the Monday of the initial week (see initial_week), else
    # the first date of the movements of the CSVs, else the first initial date of the
    # accounts, else today.
    start_date: str = field(init=False)
    today: dt.date = field(init=False)  # up to which the services are owed (see _service_rows)
    # Generated movements, like ledger CSV rows, indexed -1, -2, ...: the initial ones,
    # those of the investments, interest and conversions CSVs, the salaries and the
    # results of the accounts in other currencies, and the capital gains and losses of the
    # investments (see _capital_rows), which also have their INVESTMENT_COLUMN and BEFORE_COLUMN.
    initial_rows: DataFrame = field(init=False)
    investment_rows: DataFrame = field(init=False)
    interest_rows: DataFrame = field(init=False)
    conversion_rows: DataFrame = field(init=False)
    salary_rows: DataFrame = field(init=False)
    service_rows: DataFrame = field(init=False)  # with their SERVICE_COLUMN
    result_rows: DataFrame = field(init=False)
    capital_rows: DataFrame = field(init=False)
    # The balance of each account in another currency in that currency, and its movements
    # (see _result_rows)
    other_balances: dict[str, float] = field(init=False)
    currency_moves: dict[str, list[tuple]] = field(init=False)
    # The id of each movement by its index in all_rows: None for a CSV row without a
    # valid one yet (see stored_ids), until the ids are frozen again.
    ids: dict[int, str | None] = field(init=False)

    def __post_init__(self):
        self.today = common.today()
        dates = [
            *self.rows["move_date"], *self.investments["move_date"],
            *self.interest["move_date"], *self.conversions["move_date"],
            *(d for d in self.prices["date"] if d != INITIAL_PRICE),
        ]
        initial_dates = [a.initial_date.isoformat() for a in self.accounts.values() if a.initial_date]
        week = self.initial_week
        if week is not None:
            # The initial week only has the initial movements: the others come after it.
            self.start_date = week.start.isoformat()
            early = sorted({d for d in dates if d <= week.end.isoformat()})
            if early:
                raise ValueError(
                    f"There are movements up to the end of the initial week ({week.start} to {week.end}, the "
                    f"last one of the previous month {self.previous_month}), which only has the initial ones: {early}"
                )
        else:
            self.start_date = min(dates) if dates else min(initial_dates, default=dt.date.today().isoformat())
        self.initial_rows = _initial_rows(self, self.start_date)
        start = -len(self.initial_rows) - 1
        for name, generate in (
            ("investment_rows", _investment_rows),
            ("interest_rows", _interest_rows),
            ("conversion_rows", _conversion_rows),
            ("salary_rows", _salary_rows),
            ("service_rows", _service_rows),
        ):
            setattr(self, name, generate(self, start))
            start -= len(getattr(self, name))
        self.result_rows, self.other_balances, self.currency_moves = _result_rows(self, start)
        self.capital_rows = _capital_rows(self, start - len(self.result_rows))
        self.ids = {}
        for generated, stored, prefix in (
            (self.rows, self.rows, LEDGER_ID),
            (self.investment_rows, self.investments, INVESTMENT_ID),
            (self.interest_rows, self.interest, INTEREST_ID),
            (self.conversion_rows, self.conversions, CONVERSION_ID),
        ):
            self.ids |= zip(generated.index, stored_ids(stored, prefix))
        for generated in (self.initial_rows, self.salary_rows, self.service_rows, self.result_rows, self.capital_rows):
            self.ids |= zip(generated.index, generated[ID_COLUMN])

    @property
    def initial_week(self) -> Week | None:
        """The week of the initial movements, the last one of the previous month, if any (see initial_week)."""
        return initial_week(self.previous_month) if self.previous_month else None

    @property
    def base_currency(self) -> Currency:
        return next(iter(self.currencies.values()))

    @property
    def booked_rows(self) -> DataFrame:
        """
        The movements of the CSVs, the salaries and the services: all but the initial
        ones and the results of the accounts in other currencies and the investments.
        """
        services = self.service_rows.drop(columns=[SERVICE_COLUMN])
        return _concat(
            [self.rows, self.investment_rows, self.interest_rows, self.conversion_rows, self.salary_rows, services],
            self.rows.columns,
        )

    @cached_property
    def all_rows(self) -> DataFrame:
        """
        The initial movements of the accounts, the CSV rows, the investments, the
        interest on money, the conversions, the salaries, the results of the accounts in
        other currencies and the capital gains and losses, in date order (each date in that order).
        """
        capital = self.capital_rows.drop(columns=[INVESTMENT_COLUMN, BEFORE_COLUMN])
        rows = _concat([self.initial_rows, self.booked_rows, self.result_rows, capital], self.rows.columns)
        return rows.assign(**{ID_COLUMN: [self.ids[ix] for ix in rows.index]}).sort_values("move_date", kind="stable")

    def own_balance(self, name: str, date: str) -> float:
        """
        The balance of an account in another currency, in its currency, at the end of
        `date` (see _result_rows).
        """
        values = self.accounts[name].currency.base_currency_mapping
        return float(sum(_in_currency(m, values) for m in self.currency_moves.get(name, []) if m[0] <= date))

    def conversion_balances(self) -> dict[int, float]:
        """
        The balance in its currency of the account in another currency of each conversion,
        by its row in the conversions CSV, right after it (see _result_rows).
        """
        after = {}
        for name, moves in self.currency_moves.items():
            values = self.accounts[name].currency.base_currency_mapping
            if not len(values):
                continue
            total = 0.0
            for move in sorted(moves, key=lambda m: m[0]):  # stable: in their order on each date
                total += _in_currency(move, values)
                if move[3] is not None:
                    after[move[3]] = total
        return after

    def ledger(self, rows: DataFrame | None = None) -> Ledger:
        """Build a Ledger from these rows (all rows, initial ones included, by default)."""
        return Ledger.from_dataframe(
            self.all_rows if rows is None else rows,
            self.accounts,
            self.base_movements,
        )

    def free_accounts(self) -> list[Account]:
        """Accounts that can be picked on a non-fixed side of a movement."""
        return [a for a in self.accounts.values() if a.acc_type != AccountType.EQUITY]

    def side_accounts(self, base: MovementBase, ledger_side: LedgerSide) -> list[Account]:
        """
        Accounts that can be picked on a non-fixed side of a base movement: never an
        investment account on a free side of an investment base movement.
        """
        special = SPECIAL_SIDES.get(base.side(ledger_side))
        if special:
            return [a for a in self.accounts.values() if special(a)]
        if base.name in self.invest_base_movements():
            return [a for a in self.free_accounts() if not isinstance(a, InvestmentAccount)]
        return self.free_accounts()

    @property
    def initial_base_names(self) -> set[str]:
        """
        Base movements of the result type of the initial movements, left out of
        results by default: they set opening balances, they aren't gains or losses.
        """
        initial_rt = self.base_movements[INITIAL_GAIN].result_type
        return {n for n, b in self.base_movements.items() if b.result_type is initial_rt}

    def debt_accounts(self, month: str = "") -> list[DebtAccount]:
        """
        Copies of the debt accounts as of the end of `month` (all the ledger by default):
        their initial amounts, once they start, plus the booked movements up to then.
        """
        debts = {}
        for a in self.accounts.values():
            if isinstance(a, DebtAccount):
                started = not month or a.initial_date is None or a.initial_date.isoformat()[:7] <= month
                creditors = {n: replace(c, amount=c.amount if started else 0.0) for n, c in a.creditors.items()}
                debts[a.name] = replace(a, creditors=creditors)
        rows = self.booked_rows
        rows = rows[rows.move_date.str[:7] <= month] if month else rows
        _apply_debts({**self.accounts, **debts}, rows)
        return list(debts.values())

    def investment_accounts(
        self, unattributed: list[dict] | None = None, month: str = ""
    ) -> list[InvestmentAccount]:
        """
        Copies of the investment accounts as of the end of `month` (all the ledger by
        default), with the movements of the investments CSV and their capital gains and
        losses up to then applied to their investments (see _apply_investments). Their initial amounts
        and quantities count once they start. The results that can't be attributed
        to an investment are added to `unattributed`.
        """
        def started(a: InvestmentAccount) -> bool:
            return not month or a.initial_date is None or a.initial_date.isoformat()[:7] <= month

        accounts = {
            a.name: replace(
                a,
                investments={
                    n: dict(h) if started(a) else {**h, "total_amount": 0.0, "total_quantity": 0.0}
                    for n, h in a.investments.items()
                },
                results={n: dict(r) for n, r in a.results.items()},
            )
            for a in self.accounts.values()
            if isinstance(a, InvestmentAccount)
        }
        rows = _concat([self.investments, self.capital_rows], INVESTMENT_COLUMNS).fillna("")
        rows = rows[rows.move_date.str[:7] <= month] if month else rows
        found = _apply_investments(self, {**self.accounts, **accounts}, rows)
        if unattributed is not None:
            unattributed.extend(found)
        return list(accounts.values())

    def manual_base_movements(self) -> dict[str, MovementBase]:
        """Base movements that can be picked for a new movement (not internal or generated ones)."""
        return {n: b for n, b in self.base_movements.items() if not b.internal and n not in GENERATED}

    @property
    def conversion_base_names(self) -> set[str]:
        """The names of the base movements of the conversions: CONVERSION_BOUGHT and CONVERSION_SOLD."""
        return set(CONVERSIONS)

    def conversion_base_movements(self) -> dict[str, MovementBase]:
        """The base movements of the conversions, added from the Currencies tab: the CONVERSIONS that are internal transfers."""
        return {
            n: b for n in sorted(CONVERSIONS)
            if (b := self.base_movements.get(n)) is not None and b.internal and b.move_type == MovementType.TRANSFER
        }

    def debt_base_movements(self) -> dict[str, MovementBase]:
        """The base movements of DEBT_BASES, added from the Debt tab, that are as expected (see tab_base_movements)."""
        return self.tab_base_movements(DEBT_BASES, DEBT_ACCOUNT)[0]

    def card_payment_base_movements(self) -> dict[str, MovementBase]:
        """The base movements of CARD_PAYMENT_BASES, added from the Credit Cards tab, that are as expected (see tab_base_movements)."""
        return self.tab_base_movements(CARD_PAYMENT_BASES, CREDIT_CARD)[0]

    def tab_base_movements(
        self, expected: dict[str, LedgerSide], special: str
    ) -> tuple[dict[str, MovementBase], list[str]]:
        """
        The base movements of these names that are internal transfers with this
        special side (or fixed account) on their side, and the descriptions of the
        others, missing or not.
        """
        found, wrong = {}, []
        for name, side in expected.items():
            base = self.base_movements.get(name)
            if (
                base is not None and base.internal and base.move_type == MovementType.TRANSFER
                and base.side(side) == special
            ):
                found[name] = base
            else:
                wrong.append(f"{name!r} (an internal transfer with {side.value}: {special})")
        return found, wrong

    def correction_base_movements(self) -> dict[str, MovementBase]:
        """The base movements of the balance corrections (CORRECTIONS) that are internal ones of their type."""
        return {
            n: b for n, t in CORRECTIONS.items()
            if (b := self.base_movements.get(n)) is not None and b.internal and b.move_type == t
        }

    def ledger_base_movements(self) -> dict[str, MovementBase]:
        """The base movements that can be picked in the Ledger tab: the manual ones but those of other tabs."""
        other_tabs = (
            self.conversion_base_names | set(self.debt_base_movements()) | set(self.card_payment_base_movements())
        )
        return {n: b for n, b in self.manual_base_movements().items() if n not in other_tabs}

    def invest_base_movements(self) -> dict[str, MovementBase]:
        """
        The internal base movements handled in the Investments tab: those of the
        INVEST result type (but the CORRECTIONS) and the transfers with an INVESTMENT side. The movements of
        the INTEREST ones are in the interest CSV, and those of the others in the
        investments CSV.
        """
        return {
            n: b for n, b in self.base_movements.items()
            if b.internal and n not in CORRECTIONS and (
                b.result_type.name == INVEST if b.result_type is not None
                else INVESTMENT in {b.debit_special, b.credit_special}
            )
        }
