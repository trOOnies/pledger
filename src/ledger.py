"""ledger.py"""

from __future__ import annotations

from dataclasses import dataclass

from pandas import read_csv

from enums import MovementType
from models import Account, Movement, MovementBase


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
        df = read_csv(
            path,
            usecols=[
                "move_date", "base_movement",
                "acc_debit", "acc_credit", "amount",
            ],
            sep=";",
        )
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

    def print_data(self) -> None:
        """Print data in CLI."""
        print(40 * "-")
        print("LEDGER")
        print(
            "Move Date"
            + "\t" + "Debit"
            + "\t" + "Amount"
            + "\t" + "Credit"
        )
        for move in self.movements:
            move.print_data()
        print(40 * "-")

    def print_results(self) -> None:
        """Print results in CLI."""
        print(40 * "-")
        print("RESULTS")
        print("Gains: ", self.total_gains)
        print("Losses:", self.total_losses)
        print("Profit:", self.total_profits)
        print(40 * "-")

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
