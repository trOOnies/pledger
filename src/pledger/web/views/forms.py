"""forms.py: the fields of the movement form of the Ledger tab, and the setup forms."""

from __future__ import annotations

from pledger.enums import AccountType, LedgerSide, MovementType
from pledger.models import (
    DEBT_SIDE,
    LOAN_SIDE,
    SPECIAL_LABELS,
    MovementBase,
    is_foreign,
)
from pledger.web.store import Snapshot
from pledger.web.views.common import ACCOUNT_GROUPS, account_group


def by_group(snapshot: Snapshot, names: list[str]) -> dict[str, list[str]]:
    """
    These accounts grouped by their type, labelled (see ACCOUNT_GROUPS: general assets,
    credit cards…), in that order, each sorted alphabetically. Those in another currency
    are by currency too, in the order of the currencies, e.g. "Foreign currency assets (USD)"
    or "Debts & loans (USD)".
    """
    def key(name: str) -> tuple[str, str]:
        account = snapshot.accounts[name]
        group = account_group(account)
        if group is None:
            return AccountType.EQUITY.value, "Equity"
        label = ACCOUNT_GROUPS[group][1]
        if group.startswith("foreign_") or (group == "debt" and is_foreign(account)):
            label = f"{label} ({account.currency.name})"
        return group, label

    order = {g: i for i, g in enumerate(ACCOUNT_GROUPS)}
    currencies = {c: i for i, c in enumerate(snapshot.currencies)}
    groups: dict[tuple[str, str], list[str]] = {}
    for name in names:
        groups.setdefault(key(name), []).append(name)
    ordered = sorted(
        groups.items(),
        key=lambda item: (
            order.get(item[0][0], len(order)),
            currencies.get(snapshot.accounts[item[1][0]].currency.name, len(currencies)),
        ),
    )
    return {label: sorted(found, key=str.casefold) for (_, label), found in ordered}


def bases_by_group(snapshot: Snapshot, names: list[str]) -> dict[str, list[str]]:
    """
    These base movements grouped by their result type, labelled with its long name, in the
    order of the result types (see result_type_order), then the transfers and the internal
    transfers, each sorted alphabetically, like the Setup of the Results tab.
    """
    groups: dict[tuple[int, str], list[str]] = {}
    last = len(snapshot.result_types) + 1
    for name in names:
        base = snapshot.base_movements[name]
        if base.result_type is not None:
            key = base.result_type.id, base.result_type.long_name
        else:
            key = (last + 1, "Internal transfers") if base.internal else (last, "Transfers")
        groups.setdefault(key, []).append(name)
    return {label: sorted(found, key=str.casefold) for (_, label), found in sorted(groups.items())}


def account_fields(snapshot: Snapshot, base_name: str) -> dict:
    """
    Which debit/credit accounts are fixed or selectable for a base movement: any
    non-equity account, or only the accounts of a special side (see SPECIAL_SIDES, with
    that side's label, e.g. "Investment"), also grouped by type to pick from (see by_group).
    """
    base = snapshot.base_movements.get(base_name)
    free = [a.name for a in snapshot.free_accounts()]
    if base is None:
        return {
            "debit_fixed": None, "credit_fixed": None, "debit_options": free,
            "credit_options": free, "debit_special": None, "credit_special": None, "type": "",
            "debit_groups": by_group(snapshot, free), "credit_groups": by_group(snapshot, free),
            "blocked": snapshot.blocked_accounts(),
        }
    debit = [a.name for a in snapshot.side_accounts(base, LedgerSide.DEBIT)]
    credit = [a.name for a in snapshot.side_accounts(base, LedgerSide.CREDIT)]
    return {
        "debit_fixed": base.acc_debit.name if base.acc_debit else None,
        "credit_fixed": base.acc_credit.name if base.acc_credit else None,
        "debit_options": debit,
        "credit_options": credit,
        "debit_groups": by_group(snapshot, debit),
        "credit_groups": by_group(snapshot, credit),
        "debit_special": SPECIAL_LABELS.get(base.debit_special),
        "credit_special": SPECIAL_LABELS.get(base.credit_special),
        "type": base.move_type.value,
        "blocked": snapshot.blocked_accounts(),
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
    groups = bases_by_group(snapshot, by_type[move_type])
    bases = [name for names in groups.values() for name in names]  # in the order shown
    if base_movement not in bases:
        base_movement = bases[0] if bases else ""
    return {
        "move_type": move_type,
        "bases": bases,
        # By result type, but the transfers, which have none (see bases_by_group)
        "base_groups": groups if move_type != MovementType.TRANSFER.value else None,
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
        # The special sides a new base movement can take: not those of the debt base movements, of the People tab
        "specials": {k: v for k, v in SPECIAL_LABELS.items() if k not in (DEBT_SIDE, LOAN_SIDE)},
        "labels": SPECIAL_LABELS,
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
        # The base movements of each result type, gains and losses together, and then the transfers
        "bases_by_result_type": [
            {"result_type": rt, "bases": bases}
            for rt in (snapshot.result_types[i] for i in sorted(snapshot.result_types))
            if (bases := [b for b in snapshot.base_movements.values() if b.result_type is rt])
        ],
        "transfer_bases": [b for b in snapshot.base_movements.values() if b.move_type == MovementType.TRANSFER],
        "move_types": [t.value for t in MovementType],
        "sides": base_sides(snapshot, values["move_type"]),
        "values": values,
    }
