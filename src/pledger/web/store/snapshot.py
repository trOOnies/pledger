"""snapshot.py: everything loaded from the data files for one request."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import TYPE_CHECKING

from pandas import DataFrame

from pledger.enums import AccountType, DebtRole, LedgerSide, MovementType
from pledger.ledger import Ledger
from pledger.models import (
    CREDIT_CARD,
    INVESTMENT,
    SPECIAL_SIDES,
    Account,
    Currency,
    DebtAccount,
    InvestmentAccount,
    MovementBase,
    ResultType,
    initial_week,
    interact,
    is_foreign,
)
from pledger.web.store import common
from pledger.web.store.common import _concat, _value_on, stored_ids
from pledger.web.store.constants import (
    BEFORE_COLUMN,
    CAPITAL_GAIN,
    CAPITAL_LOSS,
    CARD_PAYMENT_BASES,
    CONVERSION_ID,
    CONVERSIONS,
    CORRECTION_GAIN,
    CORRECTION_LOSS,
    CORRECTIONS,
    DEBT_BASE_ROLES,
    DEBT_BASE_SIDES,
    DEBT_BASES,
    DIFFERENCE_GAIN,
    DIFFERENCE_LOSS,
    DIFFERENCES,
    GENERATED,
    ID_COLUMN,
    INITIAL_GAIN,
    INITIAL_LOSS,
    INITIAL_PRICE,
    INTEREST,
    INTEREST_ID,
    INVEST,
    INVESTMENT_COLUMN,
    INVESTMENT_COLUMNS,
    INVESTMENT_ID,
    LEDGER_ID,
    RESULT_GAIN,
    RESULT_LOSS,
    SALARY,
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
from pledger.web.store.services import _service_rows, service_bases
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

    def balances_today(self) -> dict[str, float]:
        """
        The balance of each account at the end of today: in its currency if it is in
        another one (see is_foreign), else in the base one.
        """
        today, rows = self.today.isoformat(), self.all_rows
        balances = self.ledger(rows[rows.move_date <= today]).balances()
        return {
            name: self.own_balance(name, today) if is_foreign(account) else balances.get(name, 0.0)
            for name, account in self.accounts.items()
        }

    def correction_of(self, account: Account, difference: float) -> tuple[str, float] | None:
        """
        The CORRECTIONS base movement and the amount in the base currency, at today's
        value of the currency, of correcting the balance of an asset or liability account
        by `difference` (in the currency of balances_today). None if under a cent.
        """
        if is_foreign(account):
            difference *= _value_on(account.currency, self.today.isoformat(), f"to correct {account.name!r} with")
        amount = round(abs(difference), 2)
        if amount < 0.005:
            return None
        gain = (difference > 0) == (account.acc_type == AccountType.ASSET)
        return CORRECTION_GAIN if gain else CORRECTION_LOSS, amount

    def debt_remaining(self, account: str, base_movement: str, amount: float, who: str) -> tuple[float, float]:
        """
        What we owe `who` in all the ledger in this debt account (negative: what they owe
        us), in its currency, and what would remain there after a movement of this one of
        DEBT_BASES of this amount, in that currency too.
        """
        balance = next(
            (a.creditors[who].amount for a in self.debt_accounts() if a.name == account and who in a.creditors), 0.0
        )
        sign = 1 if DEBT_BASES[base_movement] == LedgerSide.CREDIT else -1
        return balance, round(balance + sign * amount, 2)

    def role_accounts(self, role: DebtRole) -> list[DebtAccount]:
        """The debt accounts of this role (see DebtAccount.role), in the order of the currencies."""
        return [a for a in self.accounts.values() if isinstance(a, DebtAccount) and a.role == role]

    def base_debt_account(self, role: DebtRole) -> DebtAccount | None:
        """The debt account of this role in the base currency, if any: that of the services and companies."""
        return next((a for a in self.role_accounts(role) if a.currency.is_base_currency), None)

    def debt_account_of(self, base_movement: str, acc_debit: str, acc_credit: str) -> DebtAccount | None:
        """The debt account of the role of a debt base movement on the side it takes (see DEBT_BASE_SIDES), if any."""
        role = DEBT_BASE_ROLES.get(base_movement)
        side = acc_credit if DEBT_BASES.get(base_movement) == LedgerSide.CREDIT else acc_debit
        account = self.accounts.get(side)
        return account if isinstance(account, DebtAccount) and account.role == role else None

    @staticmethod
    def difference_of(remaining: float) -> tuple[str, float] | None:
        """The DIFFERENCES base movement and the amount settling what remains with someone (see debt_remaining). None if under a cent."""
        amount = round(abs(remaining), 2)
        if amount < 0.005:
            return None
        return DIFFERENCE_GAIN if remaining > 0 else DIFFERENCE_LOSS, amount

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
        investment account on a free side of an investment base movement, only one with an
        interest frequency for the interest on money, and only
        those that interact with the account fixed on its other side, if any.
        """
        special = SPECIAL_SIDES.get(base.side(ledger_side))
        if special:
            accounts = [a for a in self.accounts.values() if special(a)]
        elif base.name in self.invest_base_movements():
            accounts = [a for a in self.free_accounts() if not isinstance(a, InvestmentAccount)]
            if base.name in INTEREST:
                accounts = [a for a in accounts if a.interest_frequency is not None]
        else:
            accounts = self.free_accounts()
        fixed = base.acc_credit if ledger_side == LedgerSide.DEBIT else base.acc_debit
        return accounts if fixed is None else [a for a in accounts if interact(a, fixed)]

    def blocked_accounts(self) -> dict[str, list[str]]:
        """The accounts that can't be on the other side of a movement of each account, if any (see interact)."""
        free = self.free_accounts()
        blocked = {a.name: [b.name for b in free if not interact(a, b)] for a in free}
        return {name: others for name, others in blocked.items() if others}

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
        Copies of the debt accounts as of the end of `month` (all the ledger by default), in
        their order (see Account.load_yaml): their initial amounts, once they start, plus
        the booked movements up to then, each in the currency of its account (see _apply_debts).
        """
        debts = {}
        for a in self.accounts.values():
            if isinstance(a, DebtAccount):
                started = not month or a.initial_date is None or a.initial_date.isoformat()[:7] <= month
                creditors = {n: replace(c, amount=c.amount if started else 0.0) for n, c in a.creditors.items()}
                debts[a.name] = replace(a, creditors=creditors)
        rows = self.booked_rows
        rows = rows[rows.move_date.str[:7] <= month] if month else rows
        _apply_debts(self, {**self.accounts, **debts}, rows)
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
        return self.tab_base_movements(DEBT_BASES, DEBT_BASE_SIDES)[0]

    def card_payment_base_movements(self) -> dict[str, MovementBase]:
        """The base movements of CARD_PAYMENT_BASES, added from the Credit Cards tab, that are as expected (see tab_base_movements)."""
        return self.tab_base_movements(CARD_PAYMENT_BASES, CREDIT_CARD)[0]

    def tab_base_movements(
        self, expected: dict[str, LedgerSide], special: str | dict[str, str]
    ) -> tuple[dict[str, MovementBase], list[str]]:
        """
        The base movements of these names that are internal transfers with this
        special side (or fixed account; one per name, if a dict) on their side, and the
        descriptions of the others, missing or not.
        """
        found, wrong = {}, []
        for name, side in expected.items():
            own = special[name] if isinstance(special, dict) else special
            base = self.base_movements.get(name)
            if (
                base is not None and base.internal and base.move_type == MovementType.TRANSFER
                and base.side(side) == own
            ):
                found[name] = base
            else:
                wrong.append(f"{name!r} (an internal transfer with {side.value}: {own})")
        return found, wrong

    def correction_base_movements(self) -> dict[str, MovementBase]:
        """The base movements of the balance corrections (CORRECTIONS) that are internal ones of their type."""
        return {
            n: b for n, t in CORRECTIONS.items()
            if (b := self.base_movements.get(n)) is not None and b.internal and b.move_type == t
        }

    def difference_base_movements(self) -> dict[str, MovementBase]:
        """The base movements of the differences of the Debt tab (DIFFERENCES) that are internal ones of their type."""
        return {
            n: b for n, t in DIFFERENCES.items()
            if (b := self.base_movements.get(n)) is not None and b.internal and b.move_type == t
        }

    def auto_base_movements(self) -> dict[str, str]:
        """
        The base movements whose movements are created elsewhere, each with the tab that
        does it: generated by pledger (the initial amounts, the revaluations, the salaries
        and their deductions, the services and the capital gains and losses) or added
        from another tab than the Ledger one. Their movements are never added from the
        Ledger tab, even those of the non-internal ones (e.g. a deduction of the salary).
        """
        tabs = {
            INITIAL_GAIN: "Accounts", INITIAL_LOSS: "Accounts", RESULT_GAIN: "Currencies", RESULT_LOSS: "Currencies",
            SALARY: "Work", CAPITAL_GAIN: "Investments", CAPITAL_LOSS: "Investments",
        }
        auto = {n: tab for n, tab in tabs.items() if n in self.base_movements}
        auto |= {n: "Work" for n in self.work.deductions if n in self.base_movements}
        auto |= {b.name: "Services" for b in service_bases(self).values()}
        auto |= {n: "Investments" for n in self.invest_base_movements()}
        auto |= {n: "Currencies" for n in self.conversion_base_names if n in self.base_movements}
        auto |= {n: "Debt" for n in [*self.debt_base_movements(), *self.difference_base_movements()]}
        auto |= {n: "Credit Cards" for n in self.card_payment_base_movements()}
        auto |= {n: "Audit" for n in self.correction_base_movements()}
        return auto

    def ledger_base_movements(self) -> dict[str, MovementBase]:
        """The base movements that can be picked in the Ledger tab: the manual ones but those created elsewhere."""
        auto = self.auto_base_movements()
        return {n: b for n, b in self.manual_base_movements().items() if n not in auto}

    def loss_base_movements(self) -> dict[str, MovementBase]:
        """The manual loss base movements, that money is spent on: what the Budget and Trip tabs plan."""
        return {n: b for n, b in self.manual_base_movements().items() if b.move_type == MovementType.LOSS}

    def invest_base_movements(self) -> dict[str, MovementBase]:
        """
        The internal base movements handled in the Investments tab: those of the
        INVEST result type (but the CORRECTIONS and DIFFERENCES) and the transfers with an INVESTMENT side. The movements of
        the INTEREST ones are in the interest CSV, and those of the others in the
        investments CSV.
        """
        return {
            n: b for n, b in self.base_movements.items()
            if b.internal and n not in CORRECTIONS and n not in DIFFERENCES and (
                b.result_type.name == INVEST if b.result_type is not None
                else INVESTMENT in {b.debit_special, b.credit_special}
            )
        }
