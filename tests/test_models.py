"""Tests for pledger.models."""

import pytest

from pledger.enums import AccountType, MovementType
from pledger.models import Account, Movement, MovementBase


class TestAccount:
    def test_add_and_add_many(self, accounts):
        acc = accounts["Ca"]
        acc.add(10.0)
        acc.add_many([5.0, 2.5])
        assert acc.total == 17.5

    def test_equity_can_go_negative(self, accounts):
        acc = accounts["Eq"]
        acc.add(-100.0)
        assert acc.total == -100.0

    @pytest.mark.xfail(
        reason="_check_positivity result is ignored, so negatives are never rejected",
        strict=True,
    )
    def test_asset_cannot_go_negative(self, accounts):
        with pytest.raises((AssertionError, ValueError)):
            accounts["Ca"].add(-1.0)

    def test_load_yaml(self, accounts_yaml):
        accounts = Account.load_yaml(accounts_yaml)
        assert set(accounts) == {"Ca", "Bk", "Cc", "Eq"}
        assert accounts["Cc"].acc_type == AccountType.LIABILITY
        assert accounts["Bk"].description == "Bank account"

    def test_load_yaml_requires_yaml_extension(self, tmp_path):
        with pytest.raises(AssertionError):
            Account.load_yaml(str(tmp_path / "accounts.yml"))


class TestMovementBase:
    def test_move_type_and_fixed_flags(self, base_movements):
        salary = base_movements["Salary"]
        assert salary.move_type == MovementType.GAIN
        assert salary.debit_fixed and salary.credit_fixed

        move = base_movements["Move"]
        assert move.move_type == MovementType.TRANSFER
        assert not move.debit_fixed and not move.credit_fixed

    def test_load_yaml(self, accounts_yaml, base_movements_yaml):
        accounts = Account.load_yaml(accounts_yaml)
        bases = MovementBase.load_yaml(base_movements_yaml, accounts)

        assert bases["Salary"].acc_debit is accounts["Bk"]
        assert bases["Salary"].acc_credit is accounts["Eq"]
        assert bases["Gift"].acc_debit is None
        assert bases["Food"].acc_debit is accounts["Eq"]
        assert bases["Card fee"].acc_credit is accounts["Cc"]
        assert bases["Withdraw"].move_type == MovementType.TRANSFER
        assert bases["Move"].acc_debit is None and bases["Move"].acc_credit is None


class TestMovement:
    def make(self, accounts, base_movements, base, debit, credit, amount=1.0):
        return Movement(
            move_date="2026-01-01",
            move_base=base_movements[base],
            input_acc_debit=accounts[debit],
            input_acc_credit=accounts[credit],
            amount=amount,
        )

    @pytest.mark.parametrize(
        ("base", "debit", "credit"),
        [
            ("Salary", "Bk", "Eq"),
            ("Gift", "Ca", "Eq"),
            ("Food", "Eq", "Ca"),
            ("Card fee", "Eq", "Cc"),
            ("Withdraw", "Ca", "Bk"),
            ("Move", "Bk", "Cc"),
        ],
    )
    def test_valid_movements(self, accounts, base_movements, base, debit, credit):
        move = self.make(accounts, base_movements, base, debit, credit)
        assert move.move_type == base_movements[base].move_type
        assert move.acc_debit is accounts[debit]
        assert move.acc_credit is accounts[credit]

    @pytest.mark.parametrize(
        ("base", "debit", "credit"),
        [
            ("Gift", "Eq", "Ca"),  # gain recorded as loss
            ("Food", "Ca", "Eq"),  # loss recorded as gain
            ("Move", "Eq", "Ca"),  # transfer touching equity
        ],
    )
    def test_movement_type_mismatch(self, accounts, base_movements, base, debit, credit):
        with pytest.raises(AssertionError, match="Movement type must match"):
            self.make(accounts, base_movements, base, debit, credit)

    @pytest.mark.parametrize(
        ("base", "debit", "credit", "side"),
        [
            ("Salary", "Ca", "Eq", "Debit"),
            ("Card fee", "Eq", "Ca", "Credit"),
            ("Withdraw", "Bk", "Bk", "Debit"),
            ("Withdraw", "Ca", "Ca", "Credit"),
        ],
    )
    def test_fixed_account_mismatch(
        self, accounts, base_movements, base, debit, credit, side
    ):
        with pytest.raises(AssertionError, match=f"{side} account must match"):
            self.make(accounts, base_movements, base, debit, credit)
