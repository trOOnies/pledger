"""Tests for pledger.ledger."""

import pytest
from pandas import DataFrame

from pledger.enums import AccountCheck
from pledger.ledger import Ledger
from pledger.models import Movement


@pytest.fixture
def ledger(accounts, base_movements, ledger_csv) -> Ledger:
    return Ledger.from_csv(ledger_csv, accounts, base_movements)


def test_from_csv_parses_movements(ledger, accounts):
    assert len(ledger.movements) == 4
    first = ledger.movements[0]
    assert first.amount == 1000.50  # thousands separator stripped
    assert first.acc_debit is accounts["Bk"]


def test_totals(ledger):
    assert ledger.total_gains == pytest.approx(1010.50)
    assert ledger.total_losses == pytest.approx(-50.25)
    assert ledger.total_profits == pytest.approx(960.25)


def test_add_and_delete_movement(ledger, accounts, base_movements):
    loss = Movement(
        move_date="2026-01-05",
        move_base=base_movements["Food"],
        input_acc_debit=accounts["Eq"],
        input_acc_credit=accounts["Ca"],
        amount=100.0,
    )
    ledger.add_movement(loss)
    assert ledger.total_losses == pytest.approx(-150.25)
    assert ledger.total_profits == pytest.approx(860.25)

    ledger.delete_movement(-1)
    assert ledger.total_losses == pytest.approx(-50.25)
    assert ledger.total_profits == pytest.approx(960.25)

    ledger.delete_movement(0)  # the salary gain
    assert ledger.total_gains == pytest.approx(10.0)


def test_from_csv_unknown_account(tmp_path, accounts, base_movements):
    path = tmp_path / "bad.csv"
    path.write_text(
        "move_date;base_movement;acc_debit;amount;acc_credit\n"
        "2026-01-01;Gift;Nope;1;Eq\n"
    )
    with pytest.raises(ValueError, match="accounts were not found.*Nope"):
        Ledger.from_csv(str(path), accounts, base_movements)


def test_from_csv_unknown_base_movement(tmp_path, accounts, base_movements):
    path = tmp_path / "bad.csv"
    path.write_text(
        "move_date;base_movement;acc_debit;amount;acc_credit\n"
        "2026-01-01;Lottery;Ca;1;Eq\n"
    )
    with pytest.raises(ValueError, match="base movements were not found.*Lottery"):
        Ledger.from_csv(str(path), accounts, base_movements)


def test_from_dataframe_ignores_extra_columns_and_does_not_mutate(
    accounts, base_movements
):
    from pandas import DataFrame

    df = DataFrame(
        {
            "move_date": ["2026-01-01"],
            "base_movement": ["Gift"],
            "acc_debit": ["Ca"],
            "amount": ["1,234.50"],
            "acc_credit": ["Eq"],
            "comment": ["birthday"],
        }
    )
    ledger = Ledger.from_dataframe(df, accounts, base_movements)
    assert ledger.movements[0].amount == 1234.50
    assert df.loc[0, "amount"] == "1,234.50"


def test_balances(ledger):
    balances = ledger.balances()
    # Bk: +1000.50 salary, -200 withdraw
    assert balances["Bk"] == pytest.approx(800.50)
    # Ca: +200 withdraw, -50.25 food, +10 gift
    assert balances["Ca"] == pytest.approx(159.75)
    # Equity balance equals the profit
    assert balances["Eq"] == pytest.approx(ledger.total_profits)
    assert "Cc" not in balances


def ledger_of(rows, accounts, base_movements) -> Ledger:
    df = DataFrame(rows, columns=["move_date", "base_movement", "acc_debit", "acc_credit", "amount"])
    return Ledger.from_dataframe(df, accounts, base_movements)


class TestCheckAccounts:
    def test_net_positive_holds(self, ledger, accounts):
        accounts["Ca"].checks = (AccountCheck.NET_POSITIVE,)
        ledger.check_accounts()  # Ca: 200, 149.75, 159.75

    def test_net_positive_breaks(self, accounts, base_movements):
        accounts["Ca"].checks = (AccountCheck.NET_POSITIVE,)
        ledger = ledger_of(
            [("2026-01-01", "Gift", "Ca", "Eq", 10), ("2026-01-02", "Food", "Eq", "Ca", 10.5)],
            accounts, base_movements,
        )
        with pytest.raises(ValueError, match=r"'Ca' breaks its net_positive check: .* -0.50 at the end of 2026-01-02"):
            ledger.check_accounts()

    def test_net_zero_breaks(self, ledger, accounts):
        accounts["Ca"].checks = (AccountCheck.NET_ZERO,)
        with pytest.raises(ValueError, match=r"'Ca' breaks its net_zero check: .* 200.00 at the end of 2026-01-02"):
            ledger.check_accounts()

    def test_checks_the_day_as_a_whole_in_date_order(self, accounts, base_movements):
        accounts["Ca"].checks = (AccountCheck.NET_ZERO,)
        accounts["Bk"].checks = (AccountCheck.NET_POSITIVE,)
        ledger = ledger_of(
            [
                ("2026-01-02", "Move", "Bk", "Ca", 0.1),  # back to zero on the day, unordered
                ("2026-01-01", "Gift", "Bk", "Eq", 0.3),
                ("2026-01-02", "Gift", "Ca", "Eq", 0.3),
                ("2026-01-02", "Withdraw", "Ca", "Bk", 0.2),  # Bk: 0.3 + 0.1 - 0.2
                ("2026-01-02", "Food", "Eq", "Ca", 0.4),
            ],
            accounts, base_movements,
        )
        ledger.check_accounts()

    def test_liabilities_are_signed_by_their_side(self, accounts, base_movements):
        accounts["Cc"].checks = (AccountCheck.NET_POSITIVE,)
        owed = ("2026-01-01", "Card fee", "Eq", "Cc", 5)
        ledger_of([owed], accounts, base_movements).check_accounts()
        overpaid = ("2026-01-02", "Move", "Cc", "Bk", 6)
        with pytest.raises(ValueError, match="-1.00"):
            ledger_of([owed, overpaid], accounts, base_movements).check_accounts()
