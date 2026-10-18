"""Tests for pledger.models."""

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from pledger.enums import AccountType, InvestmentType, LedgerSide, MovementType
from pledger.models import (
    DEBT,
    INVESTMENT,
    Account,
    Creditor,
    Currency,
    DebtAccount,
    Investment,
    InvestmentAccount,
    Movement,
    MovementBase,
    ResultType,
    special_of,
)


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

    def test_load_yaml(self, accounts_yaml, currencies_yaml):
        accounts = Account.load_yaml(accounts_yaml, *Currency.load_yaml(currencies_yaml))
        assert set(accounts) == {"Ca", "Bk", "Cc", "Eq"}
        assert accounts["Cc"].acc_type is AccountType.LIABILITY
        assert accounts["Bk"].description == "Bank account"

    def test_load_yaml_currency_liquidity_and_initial_values(self, accounts_yaml, currencies_yaml):
        accounts = Account.load_yaml(accounts_yaml, *Currency.load_yaml(currencies_yaml))
        bk, cc = accounts["Bk"], accounts["Cc"]
        assert bk.currency.name == "USD" and not bk.currency.is_base_currency
        assert bk.is_liquid and bk.initial_amount == 100.5
        assert bk.initial_date == dt.date(2025, 12, 31)
        # Defaults: base currency, not liquid, 0 from the first ledger date
        assert cc.currency.name == "ARS" and cc.currency.is_base_currency
        assert (cc.is_liquid, cc.initial_amount, cc.initial_date) == (False, 0.0, None)
        assert accounts["Eq"].initial_amount == 0.0  # equity accounts don't set one

    @pytest.mark.parametrize(
        ("extra", "message"),
        [
            ("    currency: EUR\n", "unknown currency"),
            ("    is_liquid: maybe\n", "is_liquid must be true or false"),
            ("    initial_date: someday\n", "invalid initial date"),
            ("    initial_amount: lots\n", "initial_amount must be a number"),
            ("    initial_amount: true\n", "initial_amount must be a number"),
            ("    initial_amount:\n", "initial_amount must be a number"),
        ],
    )
    def test_load_yaml_rejects_invalid_fields(self, tmp_path, currencies_yaml, extra, message):
        path = tmp_path / "accounts.yaml"
        if "initial_amount" not in extra:
            extra += "    initial_amount: 0\n"
        path.write_text("asset:\n  Ca:\n    description: Cash\n" + extra)
        with pytest.raises(ValueError, match=message):
            Account.load_yaml(str(path), *Currency.load_yaml(currencies_yaml))

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("asset:\n  Ca:\n    description: Cash\n", "'Ca' must have an initial_amount"),
            ("liability:\n  Cc:\n    description: Card\n", "'Cc' must have an initial_amount"),
            (
                "equity:\n  Eq:\n    description: Equity\n    initial_amount: 0\n",
                "Equity account 'Eq' must not have an initial_amount",
            ),
        ],
    )
    def test_load_yaml_initial_amount_is_required_except_for_equity(
        self, tmp_path, currencies_yaml, text, message
    ):
        path = tmp_path / "accounts.yaml"
        path.write_text(text)
        with pytest.raises(ValueError, match=message):
            Account.load_yaml(str(path), *Currency.load_yaml(currencies_yaml))

    def test_load_yaml_rejects_unknown_account_type(self, tmp_path, currencies_yaml):
        path = tmp_path / "accounts.yaml"
        path.write_text("assets:\n  Ca:\n    description: Cash\n")
        with pytest.raises(ValueError, match="assets"):
            Account.load_yaml(str(path), *Currency.load_yaml(currencies_yaml))

    def test_load_yaml_requires_yaml_extension(self, tmp_path, currencies_yaml):
        with pytest.raises(AssertionError):
            Account.load_yaml(str(tmp_path / "accounts.yml"), *Currency.load_yaml(currencies_yaml))


INTERNAL_YAML = """\
internal:
  Init:
    id: 3
    long_name: Initial
    description: Account initializations
    base_movements:
      gain:
        Initial (G): Initial balance
  transfer:
    Adjust:
      description: d
      debit: Cc
"""


class TestMovementBase:
    def test_move_type_and_fixed_flags(self, base_movements):
        salary = base_movements["Salary"]
        assert salary.move_type == MovementType.GAIN
        assert salary.debit_fixed and salary.credit_fixed

        move = base_movements["Move"]
        assert move.move_type == MovementType.TRANSFER
        assert not move.debit_fixed and not move.credit_fixed

    def test_load_yaml(self, accounts_yaml, base_movements_yaml, currencies_yaml):
        accounts = Account.load_yaml(accounts_yaml, *Currency.load_yaml(currencies_yaml))
        bases, result_types = MovementBase.load_yaml(base_movements_yaml, accounts)

        assert bases["Salary"].acc_debit is accounts["Bk"]
        assert bases["Salary"].acc_credit is accounts["Eq"]
        assert bases["Gift"].acc_debit is None
        assert bases["Food"].acc_debit is accounts["Eq"]
        assert bases["Card fee"].acc_credit is accounts["Cc"]
        assert bases["Withdraw"].move_type == MovementType.TRANSFER
        assert bases["Move"].acc_debit is None and bases["Move"].acc_credit is None
        assert not any(b.internal for b in bases.values())  # internal defaults to False

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
    def test_load_yaml_rejects_invalid(
        self, tmp_path, accounts_yaml, base_movements_yaml, currencies_yaml, old, new, message
    ):
        accounts = Account.load_yaml(accounts_yaml, *Currency.load_yaml(currencies_yaml))
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


    def test_load_yaml_internal(self, tmp_path, accounts_yaml, base_movements_yaml, currencies_yaml):
        accounts = Account.load_yaml(accounts_yaml, *Currency.load_yaml(currencies_yaml))
        path = tmp_path / "result_types.yaml"
        text = Path(base_movements_yaml).read_text(encoding="utf-8")
        path.write_text(text + INTERNAL_YAML, encoding="utf-8")
        bases, result_types = MovementBase.load_yaml(str(path), accounts)
        assert bases["Adjust"].internal and not bases["Move"].internal
        assert bases["Adjust"].acc_debit is accounts["Cc"] and bases["Adjust"].result_type is None
        initial = bases["Initial (G)"]
        assert initial.internal and initial.move_type == MovementType.GAIN
        assert initial.acc_credit is accounts["Eq"]
        # Internal result types share the ids of the other ones
        init = initial.result_type
        assert (init.id, init.name, init.long_name, init.internal) == (3, "Init", "Initial", True)
        assert result_types[3] is init and not result_types[1].internal

    @pytest.mark.parametrize(
        ("old", "new", "message"),
        [
            ("internal:\n", "  Adjust2:\n    description: d\n    internal: true\ninternal:\n",
             "must not set 'internal'"),
            ("  transfer:\n    Adjust:", "  transfer:\n    Move:", "Duplicated base movement name: 'Move'"),
            ("    id: 3\n", "    id: 2\n", r"ids \(internal ones included\) must be unique and go from 1 to 3"),
            ("    id: 3\n", "    id: 4\n", r"Values seen: \[1, 2, 4\]"),
            ("  Init:\n", "  Op:\n", r"both internal and not: \['Op'\]"),
            ("      gain:\n", "      transfer:\n", "only include 'gain' and 'loss'"),
        ],
    )
    def test_load_yaml_rejects_invalid_internal(
        self, tmp_path, accounts_yaml, base_movements_yaml, currencies_yaml, old, new, message
    ):
        accounts = Account.load_yaml(accounts_yaml, *Currency.load_yaml(currencies_yaml))
        path = tmp_path / "result_types.yaml"
        assert old in INTERNAL_YAML
        text = Path(base_movements_yaml).read_text(encoding="utf-8")
        path.write_text(text + INTERNAL_YAML.replace(old, new, 1), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            MovementBase.load_yaml(str(path), accounts)

    def test_load_yaml_description_shorthand(
        self, tmp_path, accounts_yaml, base_movements_yaml, currencies_yaml
    ):
        accounts = Account.load_yaml(accounts_yaml, *Currency.load_yaml(currencies_yaml))
        path = tmp_path / "result_types.yaml"
        text = Path(base_movements_yaml).read_text(encoding="utf-8")
        text = text.replace("        Food:\n          description: Groceries\n", "        Food: Groceries\n")
        text = text.replace("  Move:\n    description: Free transfer\n", "  Move: Free transfer\n")
        path.write_text(text, encoding="utf-8")
        bases, result_types = MovementBase.load_yaml(str(path), accounts)
        food, move = bases["Food"], bases["Move"]
        assert (food.description, food.acc_debit, food.acc_credit) == ("Groceries", accounts["Eq"], None)
        assert food.result_type is result_types[1] and not food.internal
        assert (move.description, move.acc_debit, move.acc_credit) == ("Free transfer", None, None)

    def test_internal_iff_its_result_type_is(self, accounts, result_types):
        init = ResultType(1, "Init", "Initial", "", internal=True)
        assert MovementBase("Initial (L)", "", accounts["Eq"], None, init, internal=True)
        with pytest.raises(ValueError, match="internal if and only if"):
            MovementBase("Initial (L)", "", accounts["Eq"], None, init)
        with pytest.raises(ValueError, match="internal if and only if"):
            MovementBase("Food", "", accounts["Eq"], None, result_types[1], internal=True)
        with pytest.raises(ValueError, match="result type"):
            MovementBase("Initial (L)", "", accounts["Eq"], None, None, internal=True)


INVESTMENTS_YAML = """\
Br:
  BOND:
    type: private_bond
    amount: 1000.5
    quantity: 10
  T:
    type: stock
    amount: 200
    quantity: 2.5
  EUR: forex
"""


class TestInvestmentAccount:
    @pytest.fixture
    def paths(self, tmp_path, accounts_yaml, currencies_yaml):
        text = Path(accounts_yaml).read_text(encoding="utf-8")
        text = text.replace("liability:\n", "  Br:\n    description: Broker\nliability:\n")
        Path(accounts_yaml).write_text(text, encoding="utf-8")
        investments = tmp_path / "investment.yaml"
        investments.write_text(INVESTMENTS_YAML, encoding="utf-8")
        return accounts_yaml, currencies_yaml, str(investments)

    @pytest.fixture
    def broker(self, currencies) -> InvestmentAccount:
        return InvestmentAccount(
            "Br", "Broker", AccountType.ASSET, currencies["ARS"], 0.0,
            investments={
                "T": {"investment": Investment("T", "stock"), "total_amount": 200.0, "total_quantity": 2.5},
            },
        )

    def test_load_yaml(self, paths):
        accounts_yaml, currencies_yaml, investments_yaml = paths
        accounts = Account.load_yaml(accounts_yaml, *Currency.load_yaml(currencies_yaml), investments_yaml)
        br = accounts["Br"]
        assert isinstance(br, InvestmentAccount) and br.acc_type == AccountType.ASSET
        assert br.initial_amount == 1200.5
        assert br.investments["BOND"] == {
            "investment": Investment("BOND", "private_bond"), "total_amount": 1000.5, "total_quantity": 10.0,
        }
        assert br.investments["T"]["investment"].type is InvestmentType.STOCK
        assert br.investments["EUR"] == {
            "investment": Investment("EUR", InvestmentType.FOREX), "total_amount": 0.0, "total_quantity": 0.0,
        }
        assert not any(isinstance(a, InvestmentAccount) for n, a in accounts.items() if n != "Br")

    def test_load_yaml_without_investments(self, paths):
        accounts_yaml, currencies_yaml, _ = paths
        with pytest.raises(ValueError, match="'Br' must have an initial_amount"):
            Account.load_yaml(accounts_yaml, *Currency.load_yaml(currencies_yaml))

    @pytest.mark.parametrize(
        ("old", "new", "message"),
        [
            ("  EUR: forex\n", "  EUR: forex\nNope: {}\n", r"not in the accounts YAML: \['Nope'\]"),
            ("  EUR: forex\n", "  EUR: forex\nCc: {}\n", "'Cc' must be an asset account"),
            ("    quantity: 10\n", "    quantity: -1\n", "cannot be negative"),
            ("    quantity: 10\n", "    quantity: ten\n", "quantity must be a number"),
            ("    amount: 200\n", "", "amount must be a number"),
            ("    type: stock\n", "", "'T' of the account 'Br' must have a type"),
            ("    type: stock\n", "    type: crypto\n", "'T' of the account 'Br' has an unknown type: 'crypto'"),
            ("  EUR: forex\n", "  EUR: cash\n", "'EUR' of the account 'Br' has an unknown type: 'cash'"),
        ],
    )
    def test_load_yaml_rejects_invalid(self, paths, old, new, message):
        accounts_yaml, currencies_yaml, investments_yaml = paths
        text = Path(investments_yaml).read_text(encoding="utf-8")
        assert old in text
        Path(investments_yaml).write_text(text.replace(old, new, 1), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            Account.load_yaml(accounts_yaml, *Currency.load_yaml(currencies_yaml), investments_yaml)

    def test_load_yaml_rejects_initial_amount(self, paths):
        accounts_yaml, currencies_yaml, investments_yaml = paths
        text = Path(accounts_yaml).read_text(encoding="utf-8")
        text = text.replace("    description: Broker\n", "    description: Broker\n    initial_amount: 5\n")
        Path(accounts_yaml).write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match="Investment account 'Br' must not have an initial_amount"):
            Account.load_yaml(accounts_yaml, *Currency.load_yaml(currencies_yaml), investments_yaml)

    def test_add_investment(self, broker):
        broker.add_investment(Investment("T", "stock"), 100.0, 1.5)
        assert broker.investments["T"]["total_amount"] == 300.0
        assert broker.investments["T"]["total_quantity"] == 4.0
        broker.add_investment(Investment("T", "stock"), -300.0, -4.0)
        assert broker.investments["T"]["total_quantity"] == 0.0

        broker.add_investment(Investment("BOND", "private_bond"), 50.0, 5.0)
        assert broker.investments["BOND"] == {
            "investment": Investment("BOND", "private_bond"), "total_amount": 50.0, "total_quantity": 5.0,
        }

    def test_quantity_never_drops_below_zero(self, broker, currencies):
        with pytest.raises(ValueError, match="cannot be negative"):
            broker.add_investment(Investment("T", "stock"), -1.0, -2.6)
        assert broker.investments["T"]["total_quantity"] == 2.5  # unchanged
        with pytest.raises(ValueError, match="cannot be negative"):
            broker.add_investment(Investment("NEW", "stock"), 0.0, -1.0)
        assert "NEW" not in broker.investments
        broker.add_investment(Investment("T", "stock"), 0.0, -2.5 - 1e-12)  # float noise
        assert broker.investments["T"]["total_quantity"] == 0.0
        with pytest.raises(ValueError, match="cannot be negative"):
            InvestmentAccount(
                "Br", "Broker", AccountType.ASSET, currencies["ARS"], 0.0,
                investments={"T": {"investment": Investment("T", "stock"),
                                   "total_amount": 0.0, "total_quantity": -1.0}},
            )

    def test_investment_type_is_an_enum(self):
        assert Investment("T", "stock").type is InvestmentType.STOCK
        with pytest.raises(ValueError, match="'crypto' is not a valid InvestmentType"):
            Investment("T", "crypto")

    def test_investment_type_must_match(self, broker):
        with pytest.raises(ValueError, match="is a stock, not a mutual_fund"):
            broker.add_investment(Investment("T", "mutual_fund"), 1.0, 1.0)


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


class TestCreditor:
    @pytest.mark.parametrize(
        ("amount", "is_creditor", "is_debtor"),
        [
            (100.0, True, False),  # we owe them
            (-50.0, False, True),  # they owe us
            (0.0, False, False),
            (1e-12, False, False),
            (-1e-12, False, False),
            (0.1 + 0.2 - 0.3, False, False),  # float noise
        ],
    )
    def test_is_creditor_and_is_debtor(self, amount, is_creditor, is_debtor):
        creditor = Creditor("Dad", amount)
        assert (creditor.is_creditor, creditor.is_debtor) == (is_creditor, is_debtor)


CREDITORS_YAML = """\
Cc:
  Dad: 100
  Mum: -150.5
  Work: 0
"""


class TestDebtAccount:
    @pytest.fixture
    def paths(self, tmp_path, accounts_yaml, currencies_yaml):
        text = Path(accounts_yaml).read_text(encoding="utf-8")
        Path(accounts_yaml).write_text(text.replace("    initial_amount: 0\n", ""), encoding="utf-8")
        creditors = tmp_path / "creditors.yaml"
        creditors.write_text(CREDITORS_YAML, encoding="utf-8")
        return accounts_yaml, currencies_yaml, str(creditors)

    def load(self, paths):
        accounts_yaml, currencies_yaml, creditors_yaml = paths
        return Account.load_yaml(
            accounts_yaml, *Currency.load_yaml(currencies_yaml), creditors_path=creditors_yaml
        )

    def test_get_creditors_and_debtors(self, currencies):
        debts = DebtAccount(
            "De", "Debts", AccountType.LIABILITY, currencies["ARS"], 0.0,
            creditors={n: Creditor(n, a) for n, a in (("Dad", 100.0), ("Mum", -50.0), ("Work", 0.0))},
        )
        assert isinstance(debts, Account) and debts.name == "De"
        assert list(debts.get_creditors()) == ["Dad"]
        assert list(debts.get_debtors()) == ["Mum"]
        assert debts.get_creditors()["Dad"] is debts.creditors["Dad"]

    def test_total_can_be_negative(self, currencies):
        debts = DebtAccount("De", "Debts", AccountType.LIABILITY, currencies["ARS"], 0.0)
        debts.add(-50.0)
        debts.add_many([-10.0, 5.0], strict=True)
        assert debts.total == -55.0

    def test_load_yaml(self, paths):
        accounts = self.load(paths)
        cc = accounts["Cc"]
        assert isinstance(cc, DebtAccount) and cc.acc_type == AccountType.LIABILITY
        assert cc.creditors == {"Dad": Creditor("Dad", 100.0), "Mum": Creditor("Mum", -150.5), "Work": Creditor("Work", 0.0)}
        assert cc.initial_amount == -50.5
        assert not any(isinstance(a, DebtAccount) for n, a in accounts.items() if n != "Cc")

    @pytest.mark.parametrize(
        ("old", "new", "message"),
        [
            ("  Work: 0\n", "  Work: 0\nNope: {}\n", r"creditors YAML has accounts not in the accounts YAML: \['Nope'\]"),
            ("  Work: 0\n", "  Work: 0\nCa: {}\n", "Debt account 'Ca' must be a liability account"),
            ("  Dad: 100\n", "  Dad: lots\n", "Creditor 'Dad' of the account 'Cc' amount must be a number"),
            ("  Dad: 100\n", "  Dad:\n", "Creditor 'Dad' of the account 'Cc' amount must be a number"),
        ],
    )
    def test_load_yaml_rejects_invalid(self, paths, old, new, message):
        creditors_yaml = paths[2]
        text = Path(creditors_yaml).read_text(encoding="utf-8")
        assert old in text
        Path(creditors_yaml).write_text(text.replace(old, new, 1), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            self.load(paths)

    def test_load_yaml_rejects_initial_amount(self, paths):
        text = Path(paths[0]).read_text(encoding="utf-8")
        Path(paths[0]).write_text(text.replace("    description: Credit card\n", "    description: Credit card\n    initial_amount: 0\n"), encoding="utf-8")
        with pytest.raises(ValueError, match="Debt account 'Cc' must not have an initial_amount"):
            self.load(paths)

    def test_load_yaml_rejects_investment_and_debt_accounts(self, paths, tmp_path):
        investments = tmp_path / "investment.yaml"
        investments.write_text("Cc: {}\n", encoding="utf-8")
        accounts_yaml, currencies_yaml, creditors_yaml = paths
        with pytest.raises(ValueError, match=r"both investment and debt accounts: \['Cc'\]"):
            Account.load_yaml(accounts_yaml, *Currency.load_yaml(currencies_yaml), str(investments), creditors_yaml)


class TestSpecialSides:
    @pytest.fixture
    def broker(self, currencies) -> InvestmentAccount:
        return InvestmentAccount("Br", "Broker", AccountType.ASSET, currencies["ARS"], 0.0)

    @pytest.fixture
    def debts(self, currencies) -> DebtAccount:
        return DebtAccount("De", "Debts", AccountType.LIABILITY, currencies["ARS"], 0.0)

    def test_load_yaml(self, tmp_path, accounts_yaml, base_movements_yaml, currencies_yaml):
        accounts = Account.load_yaml(accounts_yaml, *Currency.load_yaml(currencies_yaml))
        path = tmp_path / "result_types.yaml"
        text = Path(base_movements_yaml).read_text(encoding="utf-8")
        text += "  Buy:\n    description: d\n    debit: INVESTMENT\n    credit: Ca\n"
        text += "  Lend:\n    description: d\n    debit: DEBT\n    credit: INVESTMENT\n"
        text = text.replace("        Gift:\n", "        Gains:\n          description: d\n          debit: INVESTMENT\n        Gift:\n")
        text = text.replace("        Food:\n", "        Forgiven:\n          description: d\n          credit: DEBT\n        Food:\n")
        path.write_text(text, encoding="utf-8")
        bases, _ = MovementBase.load_yaml(str(path), accounts)
        buy, lend, gains, forgiven = bases["Buy"], bases["Lend"], bases["Gains"], bases["Forgiven"]
        assert buy.acc_debit is None and not buy.debit_fixed and buy.debit_special == INVESTMENT
        assert buy.acc_credit is accounts["Ca"] and buy.credit_special is None
        assert buy.side(LedgerSide.DEBIT) == INVESTMENT and buy.side(LedgerSide.CREDIT) == "Ca"
        assert lend.side(LedgerSide.DEBIT) == DEBT and lend.side(LedgerSide.CREDIT) == INVESTMENT
        assert gains.move_type == MovementType.GAIN and gains.debit_special == INVESTMENT
        assert forgiven.move_type == MovementType.LOSS and forgiven.credit_special == DEBT
        assert bases["Move"].side(LedgerSide.DEBIT) is None
        specials = {"Buy", "Lend", "Gains", "Forgiven"}
        assert not any(b.debit_special or b.credit_special for n, b in bases.items() if n not in specials)

    def test_movement_requires_an_investment_account(self, accounts, broker):
        buy = MovementBase("Buy", "", None, accounts["Ca"], None, debit_special=INVESTMENT)
        move = Movement("2026-01-01", buy, broker, accounts["Ca"], 1.0)
        assert move.acc_debit is broker
        with pytest.raises(AssertionError, match="Debit account must be an investment account: Bk is not"):
            Movement("2026-01-01", buy, accounts["Bk"], accounts["Ca"], 1.0)
        sell = MovementBase("Sell", "", None, None, None, credit_special=INVESTMENT)
        with pytest.raises(AssertionError, match="Credit account must be an investment account: Cc is not"):
            Movement("2026-01-01", sell, accounts["Ca"], accounts["Cc"], 1.0)

    def test_movement_requires_a_debt_account(self, accounts, broker, debts):
        borrow = MovementBase("Borrow", "", None, None, None, credit_special=DEBT)
        move = Movement("2026-01-01", borrow, accounts["Ca"], debts, 1.0)
        assert move.acc_credit is debts
        with pytest.raises(AssertionError, match="Credit account must be a debt account: Cc is not"):
            Movement("2026-01-01", borrow, accounts["Ca"], accounts["Cc"], 1.0)
        lend = MovementBase("Lend", "", None, None, None, debit_special=DEBT, credit_special=INVESTMENT)
        assert Movement("2026-01-01", lend, debts, broker, 1.0).acc_debit is debts
        with pytest.raises(AssertionError, match="Debit account must be a debt account: Br is not"):
            Movement("2026-01-01", lend, broker, broker, 1.0)

    @pytest.mark.parametrize("special", [INVESTMENT, DEBT])
    def test_cannot_be_both_fixed_and_special(self, accounts, special):
        with pytest.raises(ValueError, match=f"both a fixed account and {special}"):
            MovementBase("Buy", "", accounts["Ca"], None, None, debit_special=special)

    def test_unknown_special(self):
        with pytest.raises(ValueError, match="unknown special side: 'NOPE'"):
            MovementBase("Buy", "", None, None, None, debit_special="NOPE")

    @pytest.mark.parametrize("name", [INVESTMENT, DEBT])
    def test_is_not_an_account_name(self, tmp_path, currencies_yaml, name):
        path = tmp_path / "accounts.yaml"
        path.write_text(f"asset:\n  {name}:\n    description: d\n    initial_amount: 0\n", encoding="utf-8")
        with pytest.raises(ValueError, match=f"'{name}' is reserved"):
            Account.load_yaml(str(path), *Currency.load_yaml(currencies_yaml))

    def test_special_of(self, accounts, broker, debts):
        assert special_of(broker) == INVESTMENT
        assert special_of(debts) == DEBT
        assert special_of(accounts["Ca"]) is None
