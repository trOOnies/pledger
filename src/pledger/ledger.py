"""ledger.py"""

from __future__ import annotations

from dataclasses import dataclass

from pandas import DataFrame, read_csv

from pledger.enums import AccountType, MovementType
from pledger.models import Account, Movement, MovementBase

LEDGER_COLUMNS = ["move_date", "base_movement", "acc_debit", "acc_credit", "amount"]


@dataclass
class Ledger:
    """Ledger of money movements."""
    movements: list[Movement]

    def __post_init__(self):
        self.total_gains = sum(
            move.amount for move in self.movements
            if move.move_type == MovementType.GAIN
        )
        self.total_losses = - sum(
            move.amount for move in self.movements
            if move.move_type == MovementType.LOSS
        )
        self.total_profits = self.total_gains + self.total_losses

    @classmethod
    def from_csv(
        cls,
        path: str,
        accounts: dict[str, Account],
        base_movements: dict[str, MovementBase],
    ) -> Ledger:
        """
        Creates a Ledger from a CSV file.
        NOTE: Must use semicolons (';').
        """
        assert path.endswith(".csv")
        df = read_csv(path, usecols=LEDGER_COLUMNS, sep=";")
        return cls.from_dataframe(df, accounts, base_movements)

    @classmethod
    def from_dataframe(
        cls,
        df: DataFrame,
        accounts: dict[str, Account],
        base_movements: dict[str, MovementBase],
    ) -> Ledger:
        """Creates a Ledger from a DataFrame with the LEDGER_COLUMNS."""
        df = df[LEDGER_COLUMNS].copy()
        df["amount"] = (
            df.amount
            .map(lambda v: v.replace(",", "").replace(" ", "") if isinstance(v, str) else v)
            .map(lambda v: 0.0 if isinstance(v, str) and v in {"", "nan"} else float(v))
            .fillna(0.0)
        )

        # Accounts
        not_seen_accounts = (
            set(df.acc_debit.unique().tolist())
            | set(df.acc_credit.unique().tolist())
        )
        not_seen_accounts = sorted([acc for acc in not_seen_accounts if acc not in accounts])
        if not_seen_accounts:
            raise ValueError(
                "The following ledger accounts were not found in the settings:"
                + str(not_seen_accounts)
            )
        del not_seen_accounts

        # BaseMovements
        not_seen_base_moves = set(df.base_movement.unique().tolist())
        not_seen_base_moves = sorted(
            [bm for bm in not_seen_base_moves if bm not in base_movements]
        )
        if not_seen_base_moves:
            raise ValueError(
                "The following ledger base movements were not found in the settings:"
                + str(not_seen_base_moves)
            )
        del not_seen_base_moves

        return cls(
            movements=[
                Movement(
                    move_date=row.move_date,
                    move_base=base_movements[row.base_movement],
                    input_acc_debit=accounts[row.acc_debit],
                    input_acc_credit=accounts[row.acc_credit],
                    amount=row.amount,
                )
                for row in df.itertuples()
            ]
        )

    def balances(self) -> dict[str, float]:
        """
        Balance per account name, signed by its natural side: assets are
        debit - credit; liabilities and equity are credit - debit.
        """
        balances: dict[str, float] = {}
        for move in self.movements:
            for acc, value in _signed_amounts(move):
                balances[acc.name] = balances.get(acc.name, 0.0) + value
        return balances

    def running_balances(self) -> list[tuple[float, float]]:
        """
        The balances (signed like in balances) of the debit and credit accounts of
        each movement, right after it, in the order of the movements.
        """
        balances: dict[str, float] = {}
        after = []
        for move in self.movements:
            for acc, value in _signed_amounts(move):
                balances[acc.name] = balances.get(acc.name, 0.0) + value
            after.append((balances[move.acc_debit.name], balances[move.acc_credit.name]))
        return after

    def check_accounts(self) -> None:
        """
        Raise if an account breaks one of its checks with its balance (signed like
        in balances) at the end of any day of the ledger, every movement of the day
        summed, in date order.
        """
        accounts: dict[str, Account] = {}
        daily: dict[str, dict[str, float]] = {}  # account name to date to its net movement
        for move in self.movements:
            for acc, value in _signed_amounts(move):
                if acc.checks:
                    accounts[acc.name] = acc
                    days = daily.setdefault(acc.name, {})
                    day = str(move.move_date)
                    days[day] = days.get(day, 0.0) + value
        for name, days in daily.items():
            balance = 0.0
            for day in sorted(days):
                balance += days[day]
                for check in accounts[name].checks:
                    if not check.holds(balance):
                        raise ValueError(
                            f"Account {name!r} breaks its {check.value} check: its balance "
                            f"is {balance:,.2f} at the end of {day}"
                        )

    def add_movement(self, movement: Movement) -> None:
        """Add a Movement to the Ledger."""
        self.movements.append(movement)
        if movement.move_type == MovementType.GAIN:
            self.total_gains += movement.amount
        elif movement.move_type == MovementType.LOSS:
            self.total_losses -= movement.amount
        self.total_profits = self.total_gains + self.total_losses

    def delete_movement(self, ix: int) -> None:
        """Add a Movement to the Ledger."""
        del_move = self.movements.pop(ix)
        if del_move.move_type == MovementType.GAIN:
            self.total_gains -= del_move.amount
        elif del_move.move_type == MovementType.LOSS:
            self.total_losses += del_move.amount
        del del_move
        self.total_profits = self.total_gains + self.total_losses


def _signed_amounts(move: Movement) -> tuple[tuple[Account, float], ...]:
    """
    The amount of a movement for each of its accounts, signed by its natural side:
    debits add to assets, credits to liabilities and equity.
    """
    return tuple(
        (acc, (sign if acc.acc_type == AccountType.ASSET else -sign) * move.amount)
        for acc, sign in ((move.acc_debit, 1), (move.acc_credit, -1))
    )
