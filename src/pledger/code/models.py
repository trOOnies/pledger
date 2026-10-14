"""code > models.py"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pledger.enums import LedgerSide


def assert_accounts(
    ledger_side: "LedgerSide",
    input_acc,
    base_move_acc,
    move_date,
) -> None:
    assert input_acc == base_move_acc, (
        f"{ledger_side.value.capitalize()} account must match: "
        f"{input_acc.name} != {base_move_acc.name} ({move_date})"
    )
