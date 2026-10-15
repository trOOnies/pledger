"""Tests for pledger.models."""

from pathlib import Path

import pandas as pd
import pytest

from pledger.enums import AccountType, MovementType
from pledger.models import Account, Currency, Movement, MovementBase, ResultType


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

    @pytest.mark.parametrize("name", ["Ca", "Cc"])  # asset and liability
    def test_add_rejects_negative_total(self, accounts, name):
        acc = accounts[name]
        acc.add(10.0)
        with pytest.raises(ValueError, match=f"'{name}' positivity"):
            acc.add(-10.01)
        assert acc.total == 10.0  # unchanged after a rejected add

    def test_add_down_to_zero_is_allowed(self, accounts):
        acc = accounts["Ca"]
        acc.add(10.0)
        acc.add(-10.0)
        assert acc.total == 0.0

    def test_emptying_an_account_tolerates_float_rounding(self, accounts):
        acc = accounts["Ca"]
        acc.add(0.3)
        acc.add(-0.1)  # total is 0.19999999999999998
        acc.add(-0.2)
        assert acc.total == pytest.approx(0.0)

        acc = accounts["Bk"]
        acc.add_many([0.3, -0.1, -0.2], strict=True)
        assert acc.total == pytest.approx(0.0)

    def test_add_many_tiny_negative_on_empty_account_is_allowed(self, accounts):
        acc = accounts["Ca"]
        acc.add_many([-1e-12], strict=True)
        assert acc.total == pytest.approx(0.0)

    @pytest.mark.parametrize("value", [0.0, 1e-12, -1e-12])
    def test_add_zero_or_near_zero_is_a_noop(self, accounts, value):
        acc = accounts["Ca"]
        acc.add(value)
        assert acc.total == 0

    def test_add_many_rejects_negative_final_total(self, accounts):
        acc = accounts["Ca"]
        acc.add(5.0)
        with pytest.raises(ValueError, match="3 added values.*'Ca' positivity"):
            acc.add_many([1.0, -2.0, -5.0])
        assert acc.total == 5.0

    def test_add_many_allows_negative_subtotals_by_default(self, accounts):
        acc = accounts["Ca"]
        acc.add_many([-5.0, 10.0])
        assert acc.total == 5.0

    def test_add_many_strict_rejects_negative_subtotals(self, accounts):
        acc = accounts["Ca"]
        acc.add(1.0)
        with pytest.raises(ValueError, match="'Ca' strict positivity"):
            acc.add_many([-5.0, 10.0], strict=True)
        assert acc.total == 1.0

    def test_add_many_strict_allows_non_negative_subtotals(self, accounts):
        acc = accounts["Ca"]
        acc.add(5.0)
        acc.add_many([-5.0, 10.0, -3.0], strict=True)
        assert acc.total == 7.0

    def test_add_many_empty_is_a_noop(self, accounts):
        acc = accounts["Ca"]
        acc.add_many([], strict=True)
        assert acc.total == 0

    def test_equity_has_no_positivity_limit(self, accounts):
        acc = accounts["Eq"]
        acc.add_many([-5.0, -10.0], strict=True)
        assert acc.total == -15.0

    def test_load_yaml(self, accounts_yaml):
        accounts = Account.load_yaml(accounts_yaml)
        assert set(accounts) == {"Ca", "Bk", "Cc", "Eq"}
        assert accounts["Cc"].acc_type is AccountType.LIABILITY
        assert accounts["Bk"].description == "Bank account"

    def test_load_yaml_rejects_unknown_account_type(self, tmp_path):
        path = tmp_path / "accounts.yaml"
        path.write_text("assets:\n  Ca:\n    description: Cash\n")
        with pytest.raises(ValueError, match="assets"):
            Account.load_yaml(str(path))

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
        bases, result_types = MovementBase.load_yaml(base_movements_yaml, accounts)

        assert bases["Salary"].acc_debit is accounts["Bk"]
        assert bases["Salary"].acc_credit is accounts["Eq"]
        assert bases["Gift"].acc_debit is None
        assert bases["Food"].acc_debit is accounts["Eq"]
        assert bases["Card fee"].acc_credit is accounts["Cc"]
        assert bases["Withdraw"].move_type == MovementType.TRANSFER
        assert bases["Move"].acc_debit is None and bases["Move"].acc_credit is None

        assert sorted(result_types) == [1, 2]
        assert result_types[1].name == "Op" and result_types[1].long_name == "Operative"
        assert bases["Salary"].result_type is result_types[1]
        assert bases["Gift"].result_type is result_types[2]
        assert bases["Withdraw"].result_type is None

    @pytest.mark.parametrize(
        ("old", "new", "message"),
        [
            ("    id: 2\n", "    id: 3\n", "must be unique and go from 1"),
            ("    id: 2\n", "    id: 1\n", "must be unique and go from 1"),
            ("      gain:\n        Gift:", "      transfer:\n        Gift:", "only include 'gain' and 'loss'"),
            ("  Move:\n", "  Food:\n", "Duplicated base movement name: 'Food'"),
            ("  Move:\n    description: Free transfer\n",
             "  Move:\n    description: Free transfer\n    debit: Eq\n", "fixed Equity account"),
        ],
    )
    def test_load_yaml_rejects_invalid(self, tmp_path, accounts_yaml, base_movements_yaml, old, new, message):
        accounts = Account.load_yaml(accounts_yaml)
        path = tmp_path / "result_types.yaml"
        text = Path(base_movements_yaml).read_text(encoding="utf-8")
        assert old in text
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            MovementBase.load_yaml(str(path), accounts)

    def test_result_type_must_match_move_type(self, accounts, result_types):
        with pytest.raises(ValueError, match="result type"):
            MovementBase("Food", "", accounts["Eq"], None, None)
        with pytest.raises(ValueError, match="result type"):
            MovementBase("Move", "", None, None, result_types[1])


class TestResultType:
    def test_id_is_one_indexed(self):
        with pytest.raises(ValueError, match="Value seen: 0"):
            ResultType(0, "Op", "Operative", "")


class TestCurrency:
    def test_load_yaml(self, tmp_path):
        (tmp_path / "currencies").mkdir()
        (tmp_path / "currencies" / "USD.csv").write_text(
            "date;unit_value_in_base_currency\n2026-01-05;1500.5\n2025-12-29;1480\n"
        )
        path = tmp_path / "currencies.yaml"
        path.write_text(
            "base_currency:\n  name: ARS\n  description: Pesos\n"
            "other_currencies:\n  - name: USD\n    description: Dollar\n"
        )
        base, others = Currency.load_yaml(str(path))
        assert base.name == "ARS" and base.is_base_currency
        usd = others["USD"]
        assert not usd.is_base_currency
        assert list(usd.base_currency_mapping) == [1480, 1500.5]  # sorted by date
        assert usd.base_currency_mapping.index[0] == pd.Timestamp("2025-12-29")

    def test_load_yaml_without_other_currencies(self, tmp_path):
        path = tmp_path / "currencies.yaml"
        path.write_text("base_currency:\n  name: ARS\n  description: Pesos\nother_currencies:\n")
        base, others = Currency.load_yaml(str(path))
        assert base.name == "ARS" and others == {}

    def test_load_yaml_rejects_bad_names(self, tmp_path):
        path = tmp_path / "currencies.yaml"
        path.write_text("base_currency:\n  name: ars\n  description: Pesos\n")
        with pytest.raises(AssertionError, match="A-Z"):
            Currency.load_yaml(str(path))


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
