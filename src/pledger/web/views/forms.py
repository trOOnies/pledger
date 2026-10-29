"""forms.py: the fields of the movement form of the Ledger tab, and the setup forms."""

from __future__ import annotations

from pledger.enums import AccountType, LedgerSide, MovementType
from pledger.models import SPECIAL_LABELS, MovementBase
from pledger.web.store import Snapshot


def account_fields(snapshot: Snapshot, base_name: str) -> dict:
    """
    Which debit/credit accounts are fixed or selectable for a base movement: any
    non-equity account, or only the accounts of a special side (see SPECIAL_SIDES, with
    that side's label, e.g. "Investment").
    """
    base = snapshot.base_movements.get(base_name)
    free = [a.name for a in snapshot.free_accounts()]
    if base is None:
        return {
            "debit_fixed": None, "credit_fixed": None, "debit_options": free,
            "credit_options": free, "debit_special": None, "credit_special": None, "type": "",
        }
    return {
        "debit_fixed": base.acc_debit.name if base.acc_debit else None,
        "credit_fixed": base.acc_credit.name if base.acc_credit else None,
        "debit_options": [a.name for a in snapshot.side_accounts(base, LedgerSide.DEBIT)],
        "credit_options": [a.name for a in snapshot.side_accounts(base, LedgerSide.CREDIT)],
        "debit_special": SPECIAL_LABELS.get(base.debit_special),
        "credit_special": SPECIAL_LABELS.get(base.credit_special),
        "type": base.move_type.value,
    }


def bases_by_type(bases: dict[str, MovementBase]) -> dict[str, list[str]]:
    """Names of these base movements, sorted and grouped by movement type (every type, even without any)."""
    return {
        t.value: [n for n in sorted(bases) if bases[n].move_type == t]
        for t in MovementType
    }


def default_move_type(by_type: dict[str, list[str]]) -> str:
    """The movement type picked by default: a loss, unless there are only base movements of other types."""
    if by_type.get(MovementType.LOSS.value):
        return MovementType.LOSS.value
    return next((t for t, names in by_type.items() if names), MovementType.LOSS.value)


def base_fields(snapshot: Snapshot, move_type: str = "", base_movement: str = "") -> dict:
    """
    Base movements of the Ledger tab to pick from for a movement type, and the
    account fields of the selected one. Without a type, it is taken from the base
    movement or defaults to a loss (see default_move_type).
    """
    ledger_bases = snapshot.ledger_base_movements()
    by_type = bases_by_type(ledger_bases)
    base = ledger_bases.get(base_movement)
    if move_type not in by_type:
        move_type = base.move_type.value if base else default_move_type(by_type)
    bases = by_type[move_type]
    if base_movement not in bases:
        base_movement = bases[0] if bases else ""
    return {
        "move_type": move_type,
        "bases": bases,
        "base_movement": base_movement,
        "fields": account_fields(snapshot, base_movement),
    }


def base_sides(snapshot: Snapshot, move_type: str) -> dict:
    """Which sides of a new base movement are equity (fixed) or selectable."""
    equity = next(
        (a.name for a in snapshot.accounts.values() if a.acc_type == AccountType.EQUITY), "Eq"
    )
    return {
        "debit_fixed": equity if move_type == MovementType.LOSS.value else None,
        "credit_fixed": equity if move_type == MovementType.GAIN.value else None,
        "free": [a.name for a in snapshot.free_accounts()],
        "specials": SPECIAL_LABELS,
        # Gains and losses are filed under a (non-internal) result type; transfers have none.
        "result_types": (
            []
            if move_type == MovementType.TRANSFER.value
            else [
                snapshot.result_types[i] for i in sorted(snapshot.result_types)
                if not snapshot.result_types[i].internal
            ]
        ),
    }


def setup_context(snapshot: Snapshot, values: dict | None = None) -> dict:
    """Result types and base movements, plus state of the setup forms."""
    values = values or {}
    values.setdefault("move_type", MovementType.LOSS.value)
    return {
        "result_types": [snapshot.result_types[i] for i in sorted(snapshot.result_types)],
        "bases_per_result_type": {
            rt.name: sum(b.result_type is rt for b in snapshot.base_movements.values())
            for rt in snapshot.result_types.values()
        },
        "bases_by_type": {
            t.value: [b for b in snapshot.base_movements.values() if b.move_type == t]
            for t in MovementType
        },
        "move_types": [t.value for t in MovementType],
        "sides": base_sides(snapshot, values["move_type"]),
        "values": values,
    }
