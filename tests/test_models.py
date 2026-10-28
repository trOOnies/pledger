"""Tests for pledger.models."""

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest
from conftest import INTERNAL_ACCOUNTS_YAML

from pledger.enums import (
    AccountCheck,
    AccountType,
    CreditorKind,
    DebtRole,
    InvestmentType,
    LedgerSide,
    MovementType,
)
from pledger.models import (
    CPI_BASE,
    CREDIT_CARD,
    FOREIGN_CURRENCY,
    INVESTMENT,
    Account,
    Company,
    Creditor,
    Currency,
    DebtAccount,
    Investment,
    InvestmentAccount,
    Movement,
    MovementBase,
    ResultType,
    WorkMonth,
    daily_cpi,
    next_month,
    parse_month,
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
        assert set(accounts) == {"Ca", "Bk", "Cc", "Fi", "De", "Eq"}
        assert [n for n, a in accounts.items() if a.internal] == ["Fi", "De", "Eq"]
        assert isinstance(accounts["De"], DebtAccount) and accounts["Fi"].checks == (AccountCheck.NET_ZERO,)
        assert accounts["Cc"].acc_type is AccountType.LIABILITY
        assert accounts["Bk"].description == "Bank account"

    def test_load_yaml_currency_liquidity_and_initial_values(self, accounts_yaml, currencies_yaml):
        accounts = Account.load_yaml(accounts_yaml, *Currency.load_yaml(currencies_yaml))
        bk, cc = accounts["Bk"], accounts["Cc"]
        assert bk.currency.name == "USD" and not bk.currency.is_base_currency
        assert bk.is_liquid and bk.initial_amount == 0.1005  # in USD
        assert bk.initial_date == dt.date(2025, 12, 31)
        # Defaults: base currency, not liquid, 0 from the first ledger date
        assert cc.currency.name == "ARS" and cc.currency.is_base_currency
        assert (cc.is_liquid, cc.initial_amount, cc.initial_date) == (False, 0.0, None)
        assert accounts["Eq"].initial_amount == 0.0  # equity accounts don't set one
        assert cc.checks == ()

    def test_load_yaml_checks(self, tmp_path, currencies_yaml):
        path = tmp_path / "accounts.yaml"
        path.write_text(
            INTERNAL_ACCOUNTS_YAML + "asset:\n  Ca:\n    description: Cash\n    initial_amount: 0\n    checks:\n    - net_positive\n"
            "  Fx:\n    description: Fictitious\n    initial_amount: 0\n    checks: [net_zero]\n"
        )
        accounts = Account.load_yaml(str(path), *Currency.load_yaml(currencies_yaml))
        assert accounts["Ca"].checks == (AccountCheck.NET_POSITIVE,)
        assert accounts["Fx"].checks == (AccountCheck.NET_ZERO,)

    @pytest.mark.parametrize(
        ("extra", "message"),
        [
            ("    checks: net_positive\n", "checks must be a list"),
            ("    checks: [positive]\n", r"unknown checks: \['positive'\]"),
            ("    checks: [net_zero, net_zero]\n", "repeats checks"),
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
        path.write_text(INTERNAL_ACCOUNTS_YAML + "asset:\n  Ca:\n    description: Cash\n" + extra)
        with pytest.raises(ValueError, match=message):
            Account.load_yaml(str(path), *Currency.load_yaml(currencies_yaml))

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("asset:\n  Ca:\n    description: Cash\n", "'Ca' must have an initial_amount"),
            ("liability:\n  Cc:\n    description: Card\n", "'Cc' must have an initial_amount"),
        ],
    )
    def test_load_yaml_initial_amount_is_required_except_for_equity(
        self, tmp_path, currencies_yaml, text, message
    ):
        path = tmp_path / "accounts.yaml"
        path.write_text(INTERNAL_ACCOUNTS_YAML + text)
        with pytest.raises(ValueError, match=message):
            Account.load_yaml(str(path), *Currency.load_yaml(currencies_yaml))
        path.write_text(INTERNAL_ACCOUNTS_YAML.replace("{description: Equity}", "{description: Equity, initial_amount: 0}"))
        with pytest.raises(ValueError, match="Equity account 'Eq' must not have an initial_amount"):
            Account.load_yaml(str(path), *Currency.load_yaml(currencies_yaml))

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("", r"must have exactly the asset Fi, the liability De and the equity Eq: it has none"),
            (INTERNAL_ACCOUNTS_YAML.replace("{Fi:", "{Fx:"), r"it has \{'Fx': 'asset', 'De': 'liability', 'Eq': 'equity'\}"),
            (INTERNAL_ACCOUNTS_YAML.replace("  liability: {De: {description: Debts}}\n", "").replace("[net_zero]}}", "[net_zero]}, De: {description: Debts}}"), "must have exactly"),
            (INTERNAL_ACCOUNTS_YAML + "equity:\n  Eq2:\n    description: d\n", r"The only equity account is the internal Eq: \['Eq2'\]"),
            (INTERNAL_ACCOUNTS_YAML + "asset:\n  Ca:\n    description: d\n    initial_amount: 0\n  Fi:\n    description: d\n",
             r"The accounts \['Fi'\] are internal"),
            (INTERNAL_ACCOUNTS_YAML.replace("{description: Debts}", "{description: Debts, currency: USD}"),
             "The internal account 'De' must be in the base currency"),
        ],
    )
    def test_load_yaml_internal_accounts(self, tmp_path, currencies_yaml, text, message):
        path = tmp_path / "accounts.yaml"
        path.write_text(text)
        with pytest.raises(ValueError, match=message):
            Account.load_yaml(str(path), *Currency.load_yaml(currencies_yaml))

    def test_load_yaml_rejects_unknown_account_type(self, tmp_path, currencies_yaml):
        path = tmp_path / "accounts.yaml"
        path.write_text(INTERNAL_ACCOUNTS_YAML + "assets:\n  Ca:\n    description: Cash\n")
        with pytest.raises(ValueError, match="assets"):
            Account.load_yaml(str(path), *Currency.load_yaml(currencies_yaml))

    def test_load_yaml_requires_yaml_extension(self, tmp_path, currencies_yaml):
        with pytest.raises(AssertionError):
            Account.load_yaml(str(tmp_path / "accounts.yml"), *Currency.load_yaml(currencies_yaml))


INTERNAL_YAML = """\
internal:
  result_types:
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
            ("      id: 3\n", "      id: 2\n", r"ids \(internal ones included\) must be unique and go from 1 to 3"),
            ("      id: 3\n", "      id: 4\n", r"Values seen: \[1, 2, 4\]"),
            ("    Init:\n", "    Op:\n", r"both internal and not: \['Op'\]"),
            ("        gain:\n", "        transfer:\n", "only include 'gain' and 'loss'"),
            ("  result_types:\n", "  types:\n", r"only has 'result_types' and 'transfer', not \['types'\]"),
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


class TestDailyCpi:
    def test_compounds_each_month_evenly_over_its_days(self):
        inflation = pd.Series([0.1, 0.1, 0.0], index=pd.period_range("2026-01", periods=3, freq="M"))
        cpi = daily_cpi(inflation)
        assert cpi.index[0] == pd.Timestamp("2025-12-31") and cpi.index[-1] == pd.Timestamp("2026-03-31")
        assert len(cpi) == 1 + 31 + 28 + 31  # every day
        assert cpi["2025-12-31"] == CPI_BASE
        assert cpi["2026-01-01"] == pytest.approx(100 * 1.1 ** (1 / 31))
        assert cpi["2026-01-31"] == pytest.approx(110)  # the whole month's inflation on its last day
        assert cpi["2026-02-14"] == pytest.approx(110 * 1.1 ** (14 / 28))
        assert cpi["2026-02-28"] == pytest.approx(121)
        assert cpi["2026-03-15"] == cpi["2026-03-31"] == pytest.approx(121)  # no inflation

    def test_negative_inflation(self):
        cpi = daily_cpi(pd.Series([-0.5], index=pd.period_range("2026-02", periods=1, freq="M")))
        assert cpi["2026-02-28"] == pytest.approx(50) and cpi["2026-02-14"] == pytest.approx(100 * 0.5 ** 0.5)

    def test_empty(self):
        assert daily_cpi(pd.Series([], dtype=float, index=pd.PeriodIndex([], freq="M"))).empty


class TestCurrency:
    YAML = (
        "base_currency:\n  name: ARS\n  description: Pesos\n"
        "other_currencies:\n  - name: USD\n    description: Dollar\n"
    )
    INFLATION = "month;monthly_inflation\n2025-12;10%\n2026-01; 2.5 %\n"

    @pytest.fixture
    def path(self, tmp_path):
        (tmp_path / "base_currency.csv").write_text(self.INFLATION)
        path = tmp_path / "currencies.yaml"
        path.write_text(self.YAML)
        return path

    def test_load_yaml(self, tmp_path, path):
        (tmp_path / "currencies.csv").write_text("date;USD\n2026-01-05;1500.5\n2025-12-29;1480\n2026-01-12;\n")
        base, others = Currency.load_yaml(str(path))
        assert base.name == "ARS" and base.is_base_currency
        assert list(base.monthly_inflation) == pytest.approx([0.1, 0.025])
        assert base.monthly_inflation.index[0] == pd.Period("2025-12", freq="M")
        assert base.customer_price_index["2026-01-31"] == pytest.approx(100 * 1.1 * 1.025)
        usd = others["USD"]
        assert not usd.is_base_currency and usd.monthly_inflation is usd.customer_price_index is None
        assert list(usd.base_currency_mapping) == [1480, 1500.5]  # sorted by date, without the empty cell
        assert usd.base_currency_mapping.index[0] == pd.Timestamp("2025-12-29")

    def test_load_yaml_without_other_currencies(self, tmp_path, path):
        path.write_text("base_currency:\n  name: ARS\n  description: Pesos\nother_currencies:\n")
        base, others = Currency.load_yaml(str(path))  # no currencies.csv needed
        assert base.name == "ARS" and others == {} and len(base.customer_price_index) == 63

    @pytest.mark.parametrize(
        ("csv", "message"),
        [
            ("date\n2026-01-01\n", r"missing the columns \['USD'\]"),
            ("date;USD;EUR\n2026-01-01;1000;1100\n", r"unknown currencies: \['EUR'\]"),
            ("date;USD\n2026-01-01;1000\n2026-01-01;1001\n", "repeats the dates"),
            ("date;USD\n2026-01-01;0\n", "must be positive"),
            ("date;USD\n2026-01-01;abc\n", "Unable to parse"),
        ],
    )
    def test_load_yaml_rejects_invalid_csv(self, tmp_path, path, csv, message):
        (tmp_path / "currencies.csv").write_text(csv)
        with pytest.raises(ValueError, match=message):
            Currency.load_yaml(str(path))

    @pytest.mark.parametrize(
        ("csv", "message"),
        [
            ("month;inflation\n2026-01;1%\n", r"missing the columns \['monthly_inflation'\]"),
            ("month;monthly_inflation\n2026-1;1%\n", "not like YYYY-MM"),
            ("month;monthly_inflation\n2026-13;1%\n", "not like YYYY-MM"),
            ("month;monthly_inflation\n2026-01;1%\n2026-03;1%\n", r"consecutive, in order, but \['2026-03'\]"),
            ("month;monthly_inflation\n2026-02;1%\n2026-01;1%\n", "consecutive"),
            ("month;monthly_inflation\n2026-01;1%\n2026-01;1%\n", "consecutive"),
            ("month;monthly_inflation\n2026-01;\n", r"no inflation for \['2026-01'\]"),
            ("month;monthly_inflation\n2026-01;-100%\n", "above -100%"),
            ("month;monthly_inflation\n2026-01;abc\n", "Unable to parse"),
        ],
    )
    def test_load_yaml_rejects_invalid_inflation(self, tmp_path, path, csv, message):
        (tmp_path / "currencies.csv").write_text("date;USD\n")
        (tmp_path / "base_currency.csv").write_text(csv)
        with pytest.raises(ValueError, match=message):
            Currency.load_yaml(str(path))

    @pytest.mark.parametrize("name", ["currencies.csv", "base_currency.csv"])
    def test_load_yaml_requires_the_csvs(self, tmp_path, path, name):
        (tmp_path / "currencies.csv").write_text("date;USD\n")
        (tmp_path / name).unlink()
        with pytest.raises(FileNotFoundError):
            Currency.load_yaml(str(path))

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
De:
  people:
    Dad: 100
    Mum: -150.5
  services:
    Work: 0
  companies:
"""


class TestDebtAccount:
    @pytest.fixture
    def paths(self, tmp_path, accounts_yaml, currencies_yaml):
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

    @pytest.mark.parametrize(
        ("comment", "name"),
        [
            ("Gas", "Gas"),
            ("Gas 2025-12 (paid by Ali)", "Gas"),
            ("gas bill", "Gas"),
            ("Bill of La Nacion", "La Nacion"),  # the longest one there
            ("La 2 and Gas", "La"),  # the earliest one
            ("Gasoline", None),  # whole words only
            ("Zurich 1/24", None),
            ("", None),
        ],
    )
    def test_creditor_in(self, currencies, comment, name):
        debts = DebtAccount(
            "De", "Debts", AccountType.LIABILITY, currencies["ARS"], 0.0,
            creditors={n: Creditor(n, 0.0) for n in ("Gas", "La", "La Nacion")},
        )
        assert debts.creditor_in(comment) == name

    def test_add_debt(self, currencies):
        debts = DebtAccount(
            "De", "Debts", AccountType.LIABILITY, currencies["ARS"], 0.0,
            creditors={"Dad": Creditor("Dad", 100.0)},
        )
        debts.add_debt("Dad", -150.0)
        debts.add_debt(None, 20.0)
        assert debts.creditors["Dad"].is_debtor and debts.creditors["Dad"].amount == -50.0
        assert debts.unassigned == 20.0

    def test_total_can_be_negative(self, currencies):
        debts = DebtAccount("De", "Debts", AccountType.LIABILITY, currencies["ARS"], 0.0)
        debts.add(-50.0)
        debts.add_many([-10.0, 5.0], strict=True)
        assert debts.total == -55.0

    def test_load_yaml(self, paths):
        accounts = self.load(paths)
        # Without a creditors YAML, De is still the debt account, with no creditors
        bare = Account.load_yaml(paths[0], *Currency.load_yaml(paths[1]))["De"]
        assert isinstance(bare, DebtAccount) and bare.creditors == {} and bare.initial_amount == 0
        cc = accounts["De"]  # the debt account, always
        assert isinstance(cc, DebtAccount) and cc.acc_type == AccountType.LIABILITY and cc.internal
        assert cc.creditors == {
            "Dad": Creditor("Dad", 100.0), "Mum": Creditor("Mum", -150.5),
            "Work": Creditor("Work", 0.0, CreditorKind.SERVICE, DebtRole.CREDITOR),
        }
        assert list(cc.get_kind(CreditorKind.SERVICE)) == ["Work"]
        assert cc.initial_amount == -50.5
        assert not any(isinstance(a, DebtAccount) for n, a in accounts.items() if n != "De")

    @pytest.mark.parametrize(
        ("old", "new", "message"),
        [
            ("  companies:\n", "  companies:\nNope: {}\n", r"only has the creditors of De, not of \['Nope'\]"),
            ("  companies:\n", "  companies:\nCc: {}\n", r"only has the creditors of De, not of \['Cc'\]"),
            ("    Dad: 100\n", "    Dad: lots\n", "Creditor 'Dad' of the account 'De' amount must be a number"),
            ("    Dad: 100\n", "    Dad:\n", "Creditor 'Dad' of the account 'De' amount must be a number"),
            ("  companies:\n", "  pets:\n", r"unknown sections \['pets'\]"),
            ("  companies:\n", "  companies: 3\n", "The companies of the account 'De' must be a mapping"),
            ("    Work: 0\n", "    Work: 0\n    Dad: 1\n", "'Dad' of the account 'De' is in both people and services"),
            ("    Work: 0\n", "    Work:\n      amount: 0\n      expected: owed\n", "unknown expected role: 'owed'"),
            ("    Work: 0\n", "    Work:\n      amount: 0\n      due: 1\n", r"unknown keys \['due'\]"),
            ("    Work: 0\n", "    Work:\n      expected: debtor\n", "'Work' of the account 'De' amount must be a number"),
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
        Path(paths[0]).write_text(text.replace("{description: Debts}", "{description: Debts, initial_amount: 0}"), encoding="utf-8")
        with pytest.raises(ValueError, match="Debt account 'De' must not have an initial_amount"):
            self.load(paths)

    def test_expected_roles(self, paths):
        creditors_yaml = paths[2]
        Path(creditors_yaml).write_text(
            "De:\n  people:\n    Dad: 1\n    Mum:\n      amount: 2\n      expected: debtor\n"
            "  services:\n    Gas: 3\n    Rent:\n      amount: 4\n      expected: null\n"
            "  companies:\n    Acme:\n      amount: 5\n      expected: creditor\n",
            encoding="utf-8",
        )
        accounts_yaml, currencies_yaml, _ = paths
        cc = Account.load_yaml(
            accounts_yaml, *Currency.load_yaml(currencies_yaml), creditors_path=creditors_yaml,
            companies=["Acme", "Globex"],
        )["De"]
        assert {n: c.expected for n, c in cc.creditors.items()} == {
            "Dad": None, "Mum": DebtRole.DEBTOR,  # people have no default
            "Gas": DebtRole.CREDITOR, "Rent": None,  # services default to creditors
            "Acme": DebtRole.CREDITOR, "Globex": DebtRole.DEBTOR,  # companies to debtors
        }
        assert cc.creditors["Mum"].amount == 2 and cc.initial_amount == 15

    @pytest.mark.parametrize(
        ("expected", "amount", "met"),
        [
            (None, 5, True), (None, -5, True),
            (DebtRole.CREDITOR, 5, True), (DebtRole.CREDITOR, 0, True), (DebtRole.CREDITOR, -5, False),
            (DebtRole.DEBTOR, -5, True), (DebtRole.DEBTOR, 0, True), (DebtRole.DEBTOR, 5, False),
            # Float residues showing as 0.00 are settled; a cent is not
            (DebtRole.DEBTOR, 1.862645149230957e-09, True), (DebtRole.DEBTOR, 0.004, True),
            (DebtRole.CREDITOR, -0.004, True), (DebtRole.DEBTOR, 0.01, False), (DebtRole.CREDITOR, -0.01, False),
        ],
    )
    def test_meets_expected(self, expected, amount, met):
        assert Creditor("X", amount, expected=expected).meets_expected is met

    def test_load_yaml_rejects_internal_investment_accounts(self, paths, tmp_path):
        investments = tmp_path / "investment.yaml"
        investments.write_text("De: {}\n", encoding="utf-8")
        accounts_yaml, currencies_yaml, creditors_yaml = paths
        with pytest.raises(ValueError, match=r"internal accounts cannot be investment accounts: \[.De.\]"):
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
        text += "  Lend:\n    description: d\n    debit: FOREIGN CURRENCY\n    credit: INVESTMENT\n"
        text = text.replace("        Gift:\n", "        Gains:\n          description: d\n          debit: INVESTMENT\n        Gift:\n")
        text = text.replace("        Food:\n", "        Forgiven:\n          description: d\n          credit: FOREIGN CURRENCY\n        Food:\n")
        path.write_text(text, encoding="utf-8")
        bases, _ = MovementBase.load_yaml(str(path), accounts)
        buy, lend, gains, forgiven = bases["Buy"], bases["Lend"], bases["Gains"], bases["Forgiven"]
        assert buy.acc_debit is None and not buy.debit_fixed and buy.debit_special == INVESTMENT
        assert buy.acc_credit is accounts["Ca"] and buy.credit_special is None
        assert buy.side(LedgerSide.DEBIT) == INVESTMENT and buy.side(LedgerSide.CREDIT) == "Ca"
        assert lend.side(LedgerSide.DEBIT) == FOREIGN_CURRENCY and lend.side(LedgerSide.CREDIT) == INVESTMENT
        assert gains.move_type == MovementType.GAIN and gains.debit_special == INVESTMENT
        assert forgiven.move_type == MovementType.LOSS and forgiven.credit_special == FOREIGN_CURRENCY
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

    def test_movement_requires_a_foreign_currency_account(self, accounts, broker, debts):
        sell = MovementBase("Sell", "", None, None, None, credit_special=FOREIGN_CURRENCY)
        move = Movement("2026-01-01", sell, accounts["Ca"], accounts["Bk"], 1.0)
        assert move.acc_credit is accounts["Bk"]
        with pytest.raises(AssertionError, match="Credit account must be a foreign currency account: Cc is not"):
            Movement("2026-01-01", sell, accounts["Ca"], accounts["Cc"], 1.0)
        lend = MovementBase("Lend", "", None, None, None, debit_special=FOREIGN_CURRENCY, credit_special=INVESTMENT)
        assert Movement("2026-01-01", lend, accounts["Bk"], broker, 1.0).acc_debit is accounts["Bk"]
        with pytest.raises(AssertionError, match="Debit account must be a foreign currency account: De is not"):
            Movement("2026-01-01", lend, debts, broker, 1.0)

    @pytest.mark.parametrize("special", [INVESTMENT, CREDIT_CARD, FOREIGN_CURRENCY])
    def test_cannot_be_both_fixed_and_special(self, accounts, special):
        with pytest.raises(ValueError, match=f"both a fixed account and {special}"):
            MovementBase("Buy", "", accounts["Ca"], None, None, debit_special=special)

    def test_unknown_special(self):
        with pytest.raises(ValueError, match="unknown special side: 'NOPE'"):
            MovementBase("Buy", "", None, None, None, debit_special="NOPE")

    @pytest.mark.parametrize("name", [INVESTMENT, CREDIT_CARD, FOREIGN_CURRENCY])
    def test_is_not_an_account_name(self, accounts_yaml, currencies_yaml, name):
        path = Path(accounts_yaml)
        path.write_text(path.read_text().replace("liability:\n", f"  {name}:\n    description: d\n    initial_amount: 0\nliability:\n"))
        with pytest.raises(ValueError, match=f"'{name}' is reserved"):
            Account.load_yaml(str(path), *Currency.load_yaml(currencies_yaml))

    def test_special_of(self, accounts, broker, debts):
        assert special_of(broker) == INVESTMENT
        assert special_of(debts) is None  # De is the debt account, but no special side takes it
        assert special_of(accounts["Ca"]) is None


def months(*rows) -> list[WorkMonth]:
    return [WorkMonth(*row) for row in rows]


class TestWorkMonth:
    @pytest.mark.parametrize("month", ["2026-13", "2026-1", "26-01", "", "2026-01-01"])
    def test_invalid_months(self, month):
        with pytest.raises(ValueError, match="must be YYYY-MM"):
            parse_month(month, "month")

    def test_next_month(self):
        assert next_month("2025-12") == "2026-01"
        assert next_month("2026-01") == "2026-02"

    def test_gap(self):
        m = WorkMonth(dt.date(2026, 5, 20))
        assert m.month == "2026-05" and m.is_gap
        with pytest.raises(ValueError, match="no job, so it cannot have a salary"):
            WorkMonth("2026-05", gross=10.0)
        with pytest.raises(ValueError, match="no job, so it cannot have a salary"):
            WorkMonth("2026-05", deductions={"Tax": 1.0})
        assert WorkMonth("2026-05", deductions={"Tax": 0.0}).net is None  # empty cells

    def test_net_is_gross_minus_deductions(self):
        m = WorkMonth("2026-01", "Acme", "Dev", 100.0, {"Tax": 20.5, "Pension": 9.5})
        assert (m.total_deductions, m.net) == (30, 70)
        assert WorkMonth("2026-01", "Acme", "Dev", 100.0).net == 100
        assert WorkMonth("2026-01", "Acme", "Dev", 100.0, {"Tax": 100.0}).net == 0

    @pytest.mark.parametrize(
        ("args", "error"),
        [
            (("Acme", None, 10.0), "both a company and a job position"),
            ((None, "Dev"), "both a company and a job position"),
            (("Acme", "Dev"), "must have a gross salary"),
            (("Acme", "Dev", None, {"Tax": 1.0}), "must have a gross salary"),
            (("Acme", "Dev", 10.0, {"Tax": 6.0, "Pension": 5.0}), "deductions of 2026-01 cannot be over its gross"),
            (("Acme", "Dev", -1.0), "cannot be negative"),
            (("Acme", "Dev", 10.0, {"Tax": -1.0}), "cannot be negative"),
        ],
    )
    def test_invalid(self, args, error):
        with pytest.raises(ValueError, match=error):
            WorkMonth("2026-01", *args)

    def test_sequence_must_have_no_missing_months(self):
        WorkMonth.check_sequence(months(("2025-12",), ("2026-01", "A", "Dev", 1.0)))
        with pytest.raises(ValueError, match="2026-02 comes after 2025-12, but 2026-01 is expected"):
            WorkMonth.check_sequence(months(("2025-12",), ("2026-02",)))
        with pytest.raises(ValueError, match="is expected"):
            WorkMonth.check_sequence(months(("2026-01",), ("2026-01",)))


class TestCompany:
    def test_from_months(self):
        companies = Company.from_months(months(
            ("2025-01", "Acme", "Jr", 1.0),
            ("2025-02", "Acme", "Sr", 2.0, {"Tax": 1.0}),
            ("2025-03",),
            ("2025-04", "Globex", "Dev", 3.0, {"Tax": 1.0}),
            ("2025-05", "Acme", "Sr", 2.0),  # back again
        ))
        assert list(companies) == ["Acme", "Globex"]  # the latest first
        acme = companies["Acme"]
        assert list(acme.positions) == ["Sr", "Jr"]
        assert acme.positions["Sr"].months == ["2025-02", "2025-05"]
        assert (acme.start, acme.end, acme.months) == ("2025-01", "2025-05", 3)
        assert companies["Globex"].positions["Dev"].start == "2025-04"
