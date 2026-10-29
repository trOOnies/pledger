"""Tests for pledger.web.store: loading and writing the data files."""

import datetime as dt
import re
from dataclasses import replace

import pytest
import yaml
from conftest import INTERNAL_ACCOUNTS_YAML
from fastapi.testclient import TestClient
from helpers import HTMX, rows, with_internal

from pledger.enums import AccountCheck
from pledger.web.app import create_app
from pledger.web.store import (
    LedgerDataError,
    LedgerStore,
    Settings,
    StaleRowError,
    _insert_yaml_entry,
    row_fingerprint,
)
from pledger.web.templating import money, quantity
from pledger.web.views import (
    account_movements,
    build_results,
    build_view,
)


class TestStore:
    def test_add_appends_one_line_and_keeps_the_rest(self, settings):
        path = settings.ledger_path
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n").rstrip(b"\r\n"))
        store = LedgerStore(settings)
        store.freeze_ids()
        before = path.read_bytes()

        store.add("2026-02-01", "Food", "Eq", "Bk", "1234.5", "x; y")

        assert path.read_bytes() == before + b"\r\n2026-02-01;Food;Eq;1,234.50;Bk;x, y;L-5"

    def test_delete_removes_only_that_line(self, settings):
        path = settings.ledger_path
        before = path.read_text().splitlines()
        store = LedgerStore(settings)
        store.delete(1, row_fingerprint(store.load().rows.iloc[1]))
        assert path.read_text().splitlines() == before[:2] + before[3:]

    def test_delete_with_stale_fingerprint_is_rejected(self, settings):
        with pytest.raises(StaleRowError):
            LedgerStore(settings).delete(0, "2000-01-01|Food|1")
        assert len(rows(settings)) == 4

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (("2026-13-01", "Food", "Eq", "Ca", "1"), "Invalid date"),
            (("2026-01-01", "Nope", "Eq", "Ca", "1"), "Unknown base movement"),
            (("2026-01-01", "Food", "Eq", "Nope", "1"), "Unknown account"),
            (("2026-01-01", "Food", "Eq", "Ca", "abc"), "Invalid amount"),
            (("2026-01-01", "Food", "Ca", "Bk", "1"), "Movement type must match"),
            (("2026-01-01", "Salary", "Ca", "Eq", "1"), "Debit account must match"),
        ],
    )
    def test_add_rejects_invalid_movements(self, settings, args, message):
        with pytest.raises(ValueError, match=message):
            LedgerStore(settings).add(*args)
        assert len(rows(settings)) == 4


class TestIds:
    def test_frozen_on_startup_keeping_every_other_byte(self, settings):
        path = settings.ledger_path
        before = path.read_text().splitlines()
        with TestClient(create_app(settings)):  # runs the startup
            pass
        assert path.read_text().splitlines() == [
            before[0] + ";id", *(f"{line};L-{i}" for i, line in enumerate(before[1:], 1))
        ]
        frozen = path.read_bytes()
        LedgerStore(settings).freeze_ids()
        assert path.read_bytes() == frozen

    def test_missing_malformed_and_repeated_ones_get_new_ids(self, settings):
        path = settings.ledger_path
        lines = path.read_text().splitlines()
        # A row copied with its id, one with a made-up id and one added without any
        path.write_text("\n".join([
            lines[0] + ";id", lines[1] + ";L-7", lines[2] + ";L-7", lines[3] + ";nope", lines[4],
        ]) + "\n")
        LedgerStore(settings).freeze_ids()
        assert [line.rsplit(";", 1)[1] for line in path.read_text().splitlines()] == ["id", "L-7", "L-8", "L-9", "L-10"]

    def test_unchanged_by_filters_and_by_deleting_other_rows(self, settings):
        store = LedgerStore(settings)
        store.freeze_ids()
        by_fingerprint = {m["fingerprint"]: m["id"] for m in build_view(store.load(), initial=True)["movements"]}
        assert len(set(by_fingerprint.values())) == len(by_fingerprint)
        for view in (build_view(store.load(), month="2026-01"), build_view(store.load(), account="Ca")):
            assert all(by_fingerprint[m["fingerprint"]] == m["id"] for m in view["movements"])
        store.delete(1, row_fingerprint(store.load().rows.iloc[1]))
        assert [m["id"] for m in build_view(store.load())["movements"]] == ["L-4", "L-3", "L-1"]
        # A new row never takes the id of one deleted before it
        store.add("2026-02-01", "Food", "Eq", "Ca", "1")
        assert build_view(store.load())["movements"][0]["id"] == "L-5"

    def test_rows_added_while_running_show_none_until_frozen(self, settings, client):
        LedgerStore(settings).freeze_ids()
        with settings.ledger_path.open("a") as f:
            f.write("2026-01-05;Food;Eq;1;Ca;\n")
        movements = client.get("/api/movements").json()
        assert [m["id"] for m in movements] == [None, "L-4", "L-3", "L-2", "L-1"]
        assert 'title="Added to the CSV while pledger runs' in client.get("/").text
        # Adding a movement freezes it first; an id sent by the client is ignored
        body = {"move_date": "2026-01-06", "base_movement": "Food", "acc_debit": "Eq",
                "acc_credit": "Ca", "amount": 2, "id": "L-1"}
        assert client.post("/api/movements", json=body).status_code == 201
        assert [m["id"] for m in client.get("/api/movements").json()][:2] == ["L-6", "L-5"]


class TestAccountChecks:
    @pytest.fixture
    def settings(self, settings):
        path = settings.accounts_path
        path.write_text(path.read_text().replace(
            "    initial_amount: 0.0\n", "    initial_amount: 0.0\n    checks:\n    - net_positive\n", 1
        ))
        return settings

    def test_load_checks_the_ledger(self, settings):
        with settings.ledger_path.open("a") as f:
            f.write("2026-01-05;Food;Eq;160;Ca;\n")
        with pytest.raises(LedgerDataError, match="'Ca' breaks its net_positive check: .* -0.25 at the end of 2026-01-05"):
            LedgerStore(settings).load()

    def test_add_breaking_a_check_is_rejected(self, settings):
        store = LedgerStore(settings)
        store.add("2026-01-05", "Food", "Eq", "Ca", "159.75")  # down to 0.00
        with pytest.raises(ValueError, match="net_positive"):
            store.add("2026-01-05", "Food", "Eq", "Ca", "0.01")
        assert len(rows(settings)) == 5

    def test_delete_breaking_a_check_is_rejected(self, settings):
        store = LedgerStore(settings)
        withdraw = store.load().rows.iloc[1]  # the 200 that Ca spends from
        with pytest.raises(ValueError, match="-50.25 at the end of 2026-01-03"):
            store.delete(1, row_fingerprint(withdraw))
        assert len(rows(settings)) == 4


class TestInternalBaseMovements:
    @pytest.fixture
    def settings(self, settings):
        # An internal transfer, with one movement at the end of the ledger
        path = settings.base_movements_path
        with_internal(path, transfers="    Adjust: Set by pledger\n")
        with settings.ledger_path.open("a") as f:
            f.write("2026-01-05;Adjust;Bk;5;Ca;\n")
        return settings

    def test_cannot_be_added(self, settings, client):
        with pytest.raises(ValueError, match="internal"):
            LedgerStore(settings).add("2026-02-01", "Adjust", "Bk", "Ca", "1")
        body = {
            "move_date": "2026-02-01", "base_movement": "Adjust",
            "acc_debit": "Bk", "acc_credit": "Ca", "amount": 1,
        }
        assert client.post("/api/movements", json=body).status_code == 422
        assert len(rows(settings)) == 5

    def test_cannot_be_picked_in_the_form(self, client):
        r = client.get("/form/bases", params={"move_type": "transfer"})
        assert "Move" in r.text and "Adjust" not in r.text
        # Not even when submitted by name
        data = {"move_date": "2026-02-01", "move_type": "transfer", "base_movement": "Adjust",
                "acc_debit": "Bk", "acc_credit": "Ca", "amount": "1"}
        r = client.post("/movements", data=data, headers=HTMX)
        assert "is internal" in r.text and ">Adjust<" not in r.text

    def test_cannot_be_deleted(self, settings, client):
        fp = row_fingerprint(rows(settings).iloc[4])
        with pytest.raises(ValueError, match="cannot be deleted"):
            LedgerStore(settings).delete(4, fp)
        assert client.post("/movements/4/delete", data={"fingerprint": fp}).status_code == 422
        assert client.delete("/api/movements/4").status_code == 422
        assert len(rows(settings)) == 5
        # The ledger shows a lock instead of the delete button
        index = client.get("/").text
        assert index.count('aria-label="Delete movement"') == 4
        assert "cannot be deleted by hand" in index
        assert client.get("/api/base-movements").json()[-1] == {
            "name": "Adjust", "description": "Set by pledger", "type": "transfer",
            "result_type": None, "debit": None, "credit": None, "internal": True,
        }


class TestMoney:
    @pytest.mark.parametrize(
        ("value", "currency", "expected"),
        [
            (1000.5, "$", "$1,000.50"),
            (-50.255, "$", "-$50.26"),
            (0, "$", "$0.00"),
            (-0.001, "$", "$0.00"),
            (1234, "ARS", "ARS 1,234.00"),
        ],
    )
    def test_money(self, value, currency, expected):
        assert money(value, currency) == expected


class TestSetupStore:
    def test_add_account_keeps_the_rest_of_the_file(self, settings):
        path = settings.accounts_path
        path.write_text(
            "# my accounts\n" + INTERNAL_ACCOUNTS_YAML + "asset:\n  Ca:\n    long_name: Cash (A)\n    description: Cash\n"
            "    initial_amount: 0\n  Bk:\n    description: Bank account\n    initial_amount: 0\n"
            "\nliability:\n  Cc:\n    description: Credit card\n    initial_amount: 0\n"
        )
        before = path.read_text()

        LedgerStore(settings).add_account("Sv", "asset", "Savings: 10%", 0.0)

        lines = before.splitlines()
        assert path.read_text().splitlines() == (
            lines[:13] + [
                "  Sv:", "    description: 'Savings: 10%'", "    currency: ARS",
                "    is_liquid: false", "    initial_amount: 0.0",
            ] + lines[13:]
        )
        sv = LedgerStore(settings).load().accounts["Sv"]
        assert sv.description == "Savings: 10%"
        assert (sv.currency.name, sv.is_liquid, sv.initial_amount, sv.initial_date) == (
            "ARS", False, 0.0, None
        )

    def test_add_account_with_currency_liquidity_and_initial_values(self, settings):
        LedgerStore(settings).add_account(
            "Sv", "asset", "Savings", "1,234.5", "USD", True, "2026-01-15"
        )
        assert "    initial_date: 2026-01-15" in settings.accounts_path.read_text()
        sv = LedgerStore(settings).load().accounts["Sv"]
        assert (sv.currency.name, sv.is_liquid, sv.initial_amount, sv.initial_date) == (
            "USD", True, 1234.5, dt.date(2026, 1, 15)
        )

    def test_add_account_with_checks(self, settings):
        store = LedgerStore(settings)
        store.add_account("Fx", "asset", "Fictitious", 0, checks=["net_zero"])
        assert "  Fx:\n    description: Fictitious\n" in settings.accounts_path.read_text()
        assert "    checks:\n    - net_zero\n" in settings.accounts_path.read_text()
        assert store.load().accounts["Fx"].checks == (AccountCheck.NET_ZERO,)

    @pytest.mark.parametrize(
        ("amount", "checks", "message"),
        [
            (0, ["positive"], "Invalid checks"),
            (0, ["net_zero", "net_zero"], "Repeated checks"),
            (5, ["net_zero"], "breaks its net_zero check: its balance is 5.00"),  # its initial movement
        ],
    )
    def test_add_account_rejects_invalid_checks(self, settings, amount, checks, message):
        before = settings.accounts_path.read_text()
        with pytest.raises(ValueError, match=message):
            LedgerStore(settings).add_account("Fx", "asset", "Fictitious", amount, checks=checks)
        assert settings.accounts_path.read_text() == before

    def test_insert_yaml_entry_into_missing_section(self):
        text = "asset:\n  Ca:\n    description: Cash\n\n"
        out = _insert_yaml_entry(text, ("liability",), "Cc", {"description": ""})
        assert out == "asset:\n  Ca:\n    description: Cash\nliability:\n  Cc:\n    description: ''\n"

    @pytest.mark.parametrize("empty", ["null", "~", "{}", "null  # none yet"])
    def test_insert_yaml_entry_into_an_empty_section(self, empty):
        text = f"internal:\n  liability:\n    De:\n      description: Debts\nliability: {empty}\n"
        out = _insert_yaml_entry(text, ("liability",), "Lo", {"description": "Loan"})
        comment = " # none yet" if "#" in empty else ""
        assert out == f"internal:\n  liability:\n    De:\n      description: Debts\nliability:{comment}\n  Lo:\n    description: Loan\n"

    def test_insert_yaml_entry_keeps_crlf_and_indent(self):
        text = "gain:\r\n    A:\r\n        description: a\r\nloss: {X: x}\r\n"
        out = _insert_yaml_entry(text, ("gain",), "B", {"description": "b"})
        assert out == "gain:\r\n    A:\r\n        description: a\r\n    B:\r\n        description: b\r\nloss: {X: x}\r\n"
        with pytest.raises(ValueError, match="edit the file by hand"):
            _insert_yaml_entry(text, ("loss",), "C", {"description": "c"})

    def test_insert_yaml_entry_nested(self):
        text = (
            "rt:\n  A:\n    bm:\n      gain:\n        G:\n          description: g\n"
            "  B:\n    id: 2\n    bm:\n      gain:\n"
            "transfer:\n  T:\n    description: t\n"
        )
        out = _insert_yaml_entry(text, ("rt", "A", "bm", "gain"), "G2", {"description": "x"})
        assert "        G:\n          description: g\n        G2:\n          description: x\n  B:" in out
        # Missing keys are created at the end of their parent block
        out = _insert_yaml_entry(text, ("rt", "A", "bm", "loss"), "L", {"description": "x"})
        assert "description: g\n      loss:\n        L:\n          description: x\n  B:" in out
        # Empty (null) sections get their first entry
        out = _insert_yaml_entry(text, ("rt", "B", "bm", "gain"), "G3", {"description": "x"})
        assert "      gain:\n        G3:\n          description: x\ntransfer:" in out
        # Only direct children match: "gain" under A is not a key of "rt"
        out = _insert_yaml_entry(text, ("rt", "gain"), "X", {"description": "x"})
        assert out.endswith("  gain:\n    X:\n      description: x\ntransfer:\n  T:\n    description: t\n")
        assert yaml.safe_load(out)["rt"]["A"]["bm"]["gain"] == {"G": {"description": "g"}}

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (("Ca", "asset", "d", 0), "already exists"),
            (("  ", "asset", "d", 0), "name is required"),
            (("A;B", "asset", "d", 0), "cannot contain"),
            (("Sv", "nope", "d", 0), "Invalid account type"),
            (("Eq2", "equity", "d", 0), "equity accounts are not supported"),
            (("Sv", "asset", "  ", 0), "description is required"),
            (("Sv", "asset", "d", 0, "EUR"), "Unknown currency"),
            (("Sv", "asset", "d", "-1"), "cannot be negative"),
            (("Sv", "asset", "d", "abc"), "Invalid initial amount"),
            (("Sv", "asset", "d", "  "), "initial amount is required"),
            (("Sv", "asset", "d", 0, "", False, "2026-13-01"), "Invalid initial date"),
        ],
    )
    def test_add_account_rejects_invalid(self, settings, args, message):
        before = settings.accounts_path.read_text()
        with pytest.raises(ValueError, match=message):
            LedgerStore(settings).add_account(*args)
        assert settings.accounts_path.read_text() == before

    def test_add_base_movement(self, settings):
        store = LedgerStore(settings)
        store.add_base_movement(
            "Rent", "loss", "Monthly rent", debit="Ca", credit="Bk", result_type="Op"
        )
        store.add_base_movement(
            "ATM", "transfer", "Cash withdrawal", debit="Ca", credit="Bk", result_type="Op"
        )
        store.add_base_movement("Gift out", "loss", "Gift to others", result_type="Gifts")
        bases = store.load().base_movements
        rent = bases["Rent"]  # debit is forced to equity for losses
        assert (rent.acc_debit.name, rent.acc_credit.name) == ("Eq", "Bk")
        assert rent.move_type.value == "loss"
        assert rent.result_type.name == "Op"
        assert (bases["ATM"].acc_debit.name, bases["ATM"].acc_credit.name) == ("Ca", "Bk")
        assert bases["ATM"].result_type is None  # transfers ignore the result type
        assert bases["Gift out"].result_type.name == "Gifts"
        data = yaml.safe_load(settings.base_movements_path.read_text())
        op = data["result_types"]["Op"]["base_movements"]
        assert op["loss"]["Rent"] == {"description": "Monthly rent", "credit": "Bk"}
        assert data["transfer"]["ATM"] == {"description": "Cash withdrawal", "debit": "Ca", "credit": "Bk"}
        gifts = data["result_types"]["Gifts"]["base_movements"]
        assert gifts["loss"] == {"Gift out": {"description": "Gift to others"}}

    def test_add_result_type(self, settings):
        store = LedgerStore(settings)
        store.add_result_type(" Tax ", "Taxes", "Taxes paid")
        result_types = store.load().result_types
        assert (result_types[4].name, result_types[4].long_name) == ("Tax", "Taxes")
        data = yaml.safe_load(settings.base_movements_path.read_text())
        assert data["result_types"]["Tax"] == {
            "id": 4, "long_name": "Taxes", "description": "Taxes paid",
        }
        # Base movements can then be added under it
        store.add_base_movement("Income tax", "loss", "Income tax", result_type="Tax")
        assert store.load().base_movements["Income tax"].result_type.name == "Tax"

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (("Op", "Operative", "d"), "already exists"),
            (("", "X", "d"), "name is required"),
            (("yes", "X", "d"), "needs quoting"),
            (("X", " ", "d"), "long name is required"),
            (("X", "X", ""), "description is required"),
        ],
    )
    def test_add_result_type_rejects_invalid(self, settings, args, message):
        before = settings.base_movements_path.read_text()
        with pytest.raises(ValueError, match=message):
            LedgerStore(settings).add_result_type(*args)
        assert settings.base_movements_path.read_text() == before

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (("Food", "loss", "d"), "already exists"),
            (("X", "nope", "d"), "Invalid movement type"),
            (("X", "loss", ""), "description is required"),
            (("X", "transfer", "d", "Ca", "Ca"), "must be different"),
            (("X", "transfer", "d", "Eq"), "non-selectable account"),
            (("X", "gain", "d", "Nope", "", "Op"), "non-selectable account"),
            (("X", "gain", "d"), "require a result type"),
            (("X", "loss", "d", "", "", "Nope"), "Unknown result type"),
        ],
    )
    def test_add_base_movement_rejects_invalid(self, settings, args, message):
        before = settings.base_movements_path.read_text()
        with pytest.raises(ValueError, match=message):
            LedgerStore(settings).add_base_movement(*args)
        assert settings.base_movements_path.read_text() == before


class TestInternalResultTypes:
    @staticmethod
    def initial_rows(settings) -> list[tuple]:
        df = LedgerStore(settings).load().initial_rows
        return list(df[["move_date", "base_movement", "acc_debit", "amount", "acc_credit"]].itertuples())

    def test_are_generated_from_the_accounts_yaml(self, settings):
        # Bk sets 100.5 on 2025-12-31; Ca and Cc start at zero
        assert self.initial_rows(settings) == [(-1, "2025-12-31", "Initial (G)", "Bk", "100.5", "Eq")]
        snapshot = LedgerStore(settings).load()
        assert len(snapshot.rows) == 4 and snapshot.ledger().balances()["Bk"] == 901.0
        assert snapshot.ledger(snapshot.rows).balances()["Bk"] == 800.5  # the CSV alone

    def test_new_accounts_get_one_without_touching_the_csv(self, settings):
        before = settings.ledger_path.read_text()
        store = LedgerStore(settings)
        store.add_account("Sv", "asset", "Savings", "1,234.5", "USD", False, "2026-01-15")
        store.add_account("Lo", "liability", "Loan", 300)
        store.add_account("Zr", "asset", "Zero", 0)
        assert settings.ledger_path.read_text() == before
        assert self.initial_rows(settings)[1:] == [
            (-2, "2026-01-15", "Initial (G)", "Sv", "1234500.0", "Eq"),  # 1,234.50 USD at 1,000
            (-3, "2026-01-01", "Initial (L)", "Eq", "300.0", "Lo"),  # on the first ledger date
        ]
        balances = store.load().ledger().balances()
        assert (balances["Sv"], balances["Lo"]) == (1234500.0, 300.0)

    def test_initial_amounts_in_other_currencies_need_their_values(self, settings):
        (settings.currencies_path.parent / "currencies.csv").write_text("date;USD\n")
        with pytest.raises(LedgerDataError, match=re.escape("There are no values of USD to set the initial amount of 'Bk'")):
            LedgerStore(settings).load()

    def test_initial_amounts_in_other_currencies_at_their_value_on_the_initial_date(self, revalued):
        (revalued.currencies_path.parent / "currencies.csv").write_text("date;USD\n2025-12-01;800\n2025-12-31;1000\n")
        snapshot = LedgerStore(revalued).load()
        initial = snapshot.initial_rows
        assert list(initial[["amount", "comment"]].itertuples(index=False)) == [("100.5", "0.1005 USD at 1,000.0000")]
        assert snapshot.other_balances["Bk"] == pytest.approx(0.1005 + (1000.5 - 200) / 1000)
        # Its initial amount is exactly in USD: no result on its initial date
        assert not any(snapshot.result_rows.move_date == "2025-12-31")

    def test_previous_month(self, settings):
        # The last week of 2025-11 is that of Thursday 2025-11-27: from Monday 2025-11-24 to Sunday 2025-11-30
        path = settings.accounts_path
        path.write_text("previous_month: 2025-11\n" + path.read_text())
        LedgerStore(settings).add_account("Lo", "liability", "Loan", 300, initial_date="2025-11-24")
        assert "Lo:\n    description: Loan\n    currency: ARS\n    is_liquid: false\n    initial_amount: 300.0\n" in path.read_text()
        snapshot = LedgerStore(settings).load()
        assert snapshot.start_date == "2025-11-24"
        assert snapshot.accounts["Lo"].initial_date == dt.date(2025, 11, 24)  # the default
        assert snapshot.accounts["Bk"].initial_date == dt.date(2025, 12, 31)  # its own, after the initial week
        assert [r[1] for r in self.initial_rows(settings)] == ["2025-12-31", "2025-11-24"]
        with pytest.raises(ValueError, match=re.escape("must be after the initial week (2025-11-24 to 2025-11-30)")):
            LedgerStore(settings).add_account("Lo2", "liability", "Loan", 300, initial_date="2025-11-30")

    def test_the_initial_week_only_has_the_initial_movements(self, settings):
        path = settings.accounts_path
        text = path.read_text()
        # The last week of 2026-01 goes from 2026-01-26 to 2026-02-01: the ledger CSV is before it
        path.write_text("previous_month: 2026-01\n" + text.replace("    initial_date: 2025-12-31\n", ""))
        with pytest.raises(LedgerDataError, match=re.escape(
            "movements up to the end of the initial week (2026-01-26 to 2026-02-01, the last one of the previous "
            "month 2026-01), which only has the initial ones: ['2026-01-01', '2026-01-02', '2026-01-03', '2026-01-04']"
        )):
            LedgerStore(settings).load()
        path.write_text("previous_month: 2025-12\n" + text.replace("2025-12-31", "2025-12-24"))
        with pytest.raises(LedgerDataError, match=re.escape(
            "Account 'Bk' cannot start (2025-12-24) before the end of the initial week (2025-12-22 to 2025-12-28"
        )):
            LedgerStore(settings).load()

    def test_previous_month_must_be_a_past_month(self, settings):
        path = settings.accounts_path
        text = path.read_text()
        month = dt.date.today().strftime("%Y-%m")
        path.write_text(f"previous_month: {month}\n" + text)
        with pytest.raises(LedgerDataError, match=re.escape(f"({month}) must be a past month")):
            LedgerStore(settings).load()
        path.write_text("previous_month: 2025-13\n" + text)
        with pytest.raises(LedgerDataError, match="must be YYYY-MM"):
            LedgerStore(settings).load()
        path.write_text("initial_date: 2025-12-22\n" + text)
        with pytest.raises(LedgerDataError, match="'initial_date' is replaced by 'previous_month'"):
            LedgerStore(settings).load()

    def test_no_revaluations_in_the_initial_week(self, revalued):
        path = revalued.accounts_path
        path.write_text("previous_month: 2025-12\n" + path.read_text().replace("    initial_date: 2025-12-31\n", ""))
        (revalued.currencies_path.parent / "currencies.csv").write_text("date;USD\n2025-12-22;1000\n2025-12-24;1100\n2026-01-05;1200\n")
        snapshot = LedgerStore(revalued).load()
        assert list(snapshot.all_rows[snapshot.all_rows.move_date <= "2025-12-28"].base_movement) == ["Initial (G)"]
        assert list(snapshot.result_rows.move_date) == ["2026-01-05"]

    def test_without_ledger_rows_start_on_the_first_initial_date(self, settings):
        settings.ledger_path.write_text("move_date;base_movement;acc_debit;amount;acc_credit;comment\n")
        LedgerStore(settings).add_account("Lo", "liability", "Loan", 300)
        assert [r[1] for r in self.initial_rows(settings)] == ["2025-12-31", "2025-12-31"]

    def test_a_negative_debt_is_a_gain(self, settings, tmp_path):
        path = tmp_path / "creditors.yaml"
        path.write_text("De:\n  people:\n    Ann: 50\n    Bob: -80\n")
        settings = replace(settings, creditors_path=path)
        assert self.initial_rows(settings)[1:] == [(-2, "2026-01-01", "Initial (G)", "De", "30.0", "Eq")]

    def test_are_hidden_by_default(self, client):
        assert all(m["base"] != "Initial (G)" for m in client.get("/api/movements").json())
        r = client.get("/")
        assert "Initial (G)" not in r.text.split('id="view"')[1] and 'name="initial" value="true" >' in r.text
        assert "Initial (G)" not in client.get("/results", headers=HTMX).text  # the charts

    def test_can_be_shown_but_not_deleted(self, client):
        movements = client.get("/api/movements", params={"initial": True}).json()
        assert movements[-1]["base"] == "Initial (G)"  # the oldest
        r = client.get("/", params={"initial": True})
        assert 'name="initial" value="true" checked>' in r.text
        assert r.text.count('hx-post="/movements/') == 4  # only the CSV rows
        assert "$100.50" in r.text  # Bk's initial movement
        assert ">Initial<" in client.get("/results", params={"initial": True}).text

    def test_cannot_be_added_by_hand(self, settings):
        with pytest.raises(ValueError, match=re.escape("'Initial (G)' is internal")):
            LedgerStore(settings).add("2026-01-05", "Initial (G)", "Ca", "Eq", "5")

    def test_cannot_be_in_the_ledger_csv(self, settings, client):
        with settings.ledger_path.open("a") as f:
            f.write("2026-01-05;Initial (G);Ca;5;Eq;\n")
        with pytest.raises(LedgerDataError, match=re.escape("cannot have movements of ['Initial (G)']")):
            LedgerStore(settings).load()
        assert client.get("/").status_code == 500

    @pytest.mark.parametrize(
        ("old", "new", "missing"),
        [
            ("          Initial (G): Initial balance\n", "", "Initial (G)"),
            ("          Initial (L): Initial balance\n", "", "Initial (L)"),
            ("        gain:\n          Initial (G)", "        loss:\n          Initial (G)", "Initial (G)"),  # a loss
            ("          Initial (G): Initial balance\n",
             "          Initial (G):\n            description: d\n            debit: Ca\n", "Initial (G)"),
        ],
    )
    def test_are_required_to_load(self, settings, client, old, new, missing):
        path = settings.base_movements_path
        assert old in path.read_text()
        path.write_text(path.read_text().replace(old, new, 1))
        with pytest.raises(LedgerDataError, match=re.escape(f"base movement {missing!r} is required")):
            LedgerStore(settings).load()
        r = client.get("/")
        assert r.status_code == 500 and f"{missing!r} is required" in r.text.replace("&#39;", "'")

    def test_cannot_get_new_base_movements(self, settings, client):
        with pytest.raises(ValueError, match="'Init' is internal"):
            LedgerStore(settings).add_base_movement("X", "gain", "d", result_type="Init")
        r = client.get("/form/base-sides", params={"move_type": "gain"})
        assert ">Operative<" in r.text and ">Initial<" not in r.text

    def test_share_ids_with_the_other_result_types(self, settings, client):
        LedgerStore(settings).add_result_type("New", "New one", "d")
        result_types = client.get("/api/result-types").json()
        assert [(rt["id"], rt["name"], rt["internal"]) for rt in result_types] == [
            (1, "Op", False), (2, "Gifts", False), (3, "Init", True), (4, "New", False),
        ]


def test_quantity_format():
    assert quantity(1000) == "1,000"
    assert quantity(1.5) == "1.5"
    assert quantity(0.123456) == "0.1235"
    assert quantity(0) == "0"


def test_missing_files_are_created(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings.from_env()
    created = settings.create_missing_files()
    assert created == list(settings.expected_files())
    assert all(p.is_file() for p in created) and (tmp_path / "info" / "movements").is_dir()
    assert settings.ledger_path.read_text() == "move_date;base_movement;acc_debit;amount;acc_credit;comment;id\n"
    assert settings.accounts_path.read_text() == ""
    settings.accounts_path.write_text("asset: {}\n")
    assert settings.create_missing_files() == []  # nothing missing, nothing touched
    assert settings.accounts_path.read_text() == "asset: {}\n"


def test_startup_aborts_with_missing_files(tmp_path, monkeypatch, capsys):
    from pledger.web import __main__

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["pledger-web"])
    monkeypatch.setattr(__main__.uvicorn, "run", lambda *a, **k: pytest.fail("pledger started"))
    with pytest.raises(SystemExit) as e:
        __main__.main()
    assert "info/accounts.yaml" in str(e.value) and "were created empty" in str(e.value)
    # They were all created, so it starts now
    started = []
    monkeypatch.setattr(__main__.uvicorn, "run", lambda *a, **k: started.append(a))
    __main__.main()
    assert started


class TestRunningTotals:
    def test_credit_card_balance(self, settings):
        with settings.ledger_path.open("a") as f:
            f.write("2026-01-05;Card fee;Eq;5;Cc;\n")
        moves = account_movements(LedgerStore(settings).load(), ["Cc"])
        assert [(m["base"], m["balance"]) for m in moves if not m["initial"]] == [("Card fee", pytest.approx(5))]

    def test_cumulative_results(self, settings):
        gains, _, profits = build_results(LedgerStore(settings).load())["cumulative_results"]
        assert [g["value"] for g in gains] == [pytest.approx(1000.5), pytest.approx(1010.5)]
        assert [g["share"] for g in gains] == [pytest.approx(99.0), pytest.approx(100.0)]
        assert [p["value"] for p in profits] == [pytest.approx(950.25), pytest.approx(960.25)]
        # On the scale of the salary, the largest one
        assert profits[1]["share"] is None and profits[1]["width"] == pytest.approx(100 * 960.25 / 1000.5)
        assert [c["value"] for c in profits[0]["children"]][:2] == [pytest.approx(1000.5), pytest.approx(-50.25)]

    def test_cumulative_toggle(self, client):
        r = client.get("/results")
        assert 'name="results-cumulative"' in r.text
        assert '<div class="composition-pair" data-cumulative="true">' in r.text
        assert '<div class="composition-pair" data-cumulative="false">' in r.text


class TestSnapshotCache:
    def test_reused_while_the_files_are_unchanged(self, settings):
        store = LedgerStore(settings)
        assert store.load() is store.load()

    def test_reloaded_when_a_file_changes(self, settings):
        store = LedgerStore(settings)
        first = store.load()
        store.add("2026-01-05", "Gift", "Ca", "Eq", "5")
        second = store.load()
        assert second is not first and len(second.rows) == len(first.rows) + 1
        with settings.ledger_path.open("a") as f:  # by hand, while pledger runs
            f.write("2026-01-06;Gift;Ca;7;Eq;\n")
        assert len(store.load().rows) == len(second.rows) + 1

    def test_reloaded_on_another_day(self, settings, monkeypatch):
        store = LedgerStore(settings)
        first = store.load()
        monkeypatch.setattr("pledger.web.store.common.today", lambda: dt.date(2030, 1, 1))
        assert store.load() is not first and store.load().today == dt.date(2030, 1, 1)

    def test_errors_are_not_cached(self, settings):
        store = LedgerStore(settings)
        good = settings.ledger_path.read_text()
        settings.ledger_path.write_text(good + "2026-01-05;Nope;Ca;1;Eq;\n")
        with pytest.raises(LedgerDataError):
            store.load()
        settings.ledger_path.write_text(good)
        assert store.load() is store.load()
