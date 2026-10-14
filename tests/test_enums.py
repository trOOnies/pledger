"""Tests for pledger.enums."""

import pytest

from pledger.enums import AccountType, MovementType


@pytest.mark.parametrize(
    ("debit", "credit", "expected"),
    [
        ("Bk", "Eq", MovementType.GAIN),
        (None, "Eq", MovementType.GAIN),
        ("Eq", "Ca", MovementType.LOSS),
        ("Eq", None, MovementType.LOSS),
        ("Ca", "Bk", MovementType.TRANSFER),
        (None, None, MovementType.TRANSFER),
    ],
)
def test_movement_type_from_accounts(accounts, debit, credit, expected):
    result = MovementType.from_accounts(
        acc_debit=accounts.get(debit),
        acc_credit=accounts.get(credit),
    )
    assert result == expected


def test_enums_compare_equal_to_strings():
    # YAML keys are plain strings; models rely on this equality.
    assert AccountType.EQUITY == "equity"
    assert MovementType.GAIN == "gain"
