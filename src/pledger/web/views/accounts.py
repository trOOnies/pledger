"""accounts.py: the Accounts tab: balances, compositions, ratios and the account forms."""

from __future__ import annotations

from pledger.enums import AccountCheck, AccountType, InterestFrequency
from pledger.models import (
    SPECIAL_LABELS,
    CreditCardAccount,
    DebtAccount,
    InvestmentAccount,
    special_of,
)
from pledger.web.store import Snapshot
from pledger.web.views.charts import line_chart
from pledger.web.views.common import (
    ACCOUNT_GROUPS,
    account_group,
    grouped_composition,
    month_end_balances,
    months_of,
    own_balance,
    percent,
    ratio_series,
)

ACCOUNT_TYPE_ORDER = list(AccountType)


LIQUIDITY_GROUPS = {True: "Liquid", False: "Illiquid"}


EDITABLE_ACCOUNT_TYPES = {AccountType.ASSET, AccountType.LIABILITY}


def account_editing(account) -> dict:
    """
    What the account form needs to edit an account: its name and type, which stay, where
    its initial amount comes from if not from the form (its investments or creditors), and
    whether it is a credit card account, always liquid.
    """
    return {
        "name": account.name,
        "type": account.acc_type.value,
        "amount_from": (
            "investments" if isinstance(account, InvestmentAccount)
            else "creditors" if isinstance(account, DebtAccount)
            else None
        ),
        "credit_card": isinstance(account, CreditCardAccount),
    }


def account_values(account) -> dict:
    """The values of the account form for an account, to edit it."""
    return {
        "acc_type": account.acc_type.value,
        "acc_name": account.name,
        "acc_description": account.description,
        "acc_currency": account.currency.name,
        "acc_is_liquid": account.is_liquid,
        # In its currency: with the decimals of the amounts in other currencies (see the conversions)
        "acc_initial_amount": f"{account.initial_amount:,.{2 if account.currency.is_base_currency else 4}f}",
        "acc_initial_date": account.initial_date.isoformat() if account.initial_date else "",
        "acc_check": account.checks[0].value if account.checks else "",
        "acc_interest_frequency": account.interest_frequency.value if account.interest_frequency else "",
    }


def build_accounts(snapshot: Snapshot, month: str = "", values: dict | None = None) -> dict:
    """
    Balances per account and per type, their composition and the month-end ratios
    over equity, plus the state of the new account form.
    """
    rows = snapshot.all_rows
    # Balances are cumulative: as of the end of the selected month.
    upto = rows[rows.move_date.str[:7] <= month] if month else rows
    balances = snapshot.ledger(upto).balances()

    accounts_by_type = {
        t.value: [
            {
                "name": a.name,
                "type": a.acc_type.value,
                "description": a.description,
                "currency": a.currency.name,
                "is_liquid": a.is_liquid,
                "special": (
                    "Debt" if isinstance(a, DebtAccount) else "Credit card" if isinstance(a, CreditCardAccount)
                    else SPECIAL_LABELS.get(special_of(a), "-")
                ),
                "checks": [CHECK_LABELS[c] for c in a.checks],
                "balance": balances.get(a.name, 0.0),
                "own": own_balance(snapshot, a, month),
                "editable": a.acc_type in EDITABLE_ACCOUNT_TYPES,
                "internal": a.internal,
            }
            for a in snapshot.accounts.values()
            if a.acc_type == t
        ]
        for t in ACCOUNT_TYPE_ORDER
    }
    totals = {t: sum(a["balance"] for a in accs) for t, accs in accounts_by_type.items()}
    # The balances tables: one per group of the asset and liability accounts, then the equity ones
    group_of = {a.name: account_group(a) or a.acc_type.value for a in snapshot.accounts.values()}
    groups = {**ACCOUNT_GROUPS, AccountType.EQUITY.value: ("⚖️", "Equity")}
    account_groups = [
        {"key": key, "emoji": emoji, "label": label, "type": accs[0]["type"], "accounts": accs,
         "has_own": any(a["own"] is not None for a in accs)}
        for key, (emoji, label) in groups.items()
        if (accs := [a for t in accounts_by_type.values() for a in t if group_of[a["name"]] == key])
    ]

    return {
        "months": months_of(snapshot),
        "month": month,
        "assets": totals[AccountType.ASSET.value],
        "liabilities": totals[AccountType.LIABILITY.value],
        "equity": totals[AccountType.EQUITY.value],
        "accounts_by_type": accounts_by_type,
        "account_groups": account_groups,
        "has_own": {t: any(a["own"] is not None for a in accs) for t, accs in accounts_by_type.items()},
        "groupings": balance_groupings(snapshot, balances),
        **over_time_charts(snapshot, month),
        **account_form(snapshot, values),
    }


def account_form(snapshot: Snapshot, values: dict | None = None, editing: dict | None = None) -> dict:
    """The state of the account form: of a new account, or of an account `editing` (see account_editing)."""
    values = values or {}
    values.setdefault("acc_type", AccountType.ASSET.value)
    values.setdefault("acc_currency", snapshot.base_currency.name)
    values.setdefault("acc_initial_amount", "0.00")
    return {
        # New equity accounts are not supported for now.
        "account_types": [t.value for t in ACCOUNT_TYPE_ORDER if t != AccountType.EQUITY],
        "currencies": list(snapshot.currencies.values()),
        "check_labels": CHECK_LABELS,
        "interest_frequencies": [f.value for f in InterestFrequency],
        "values": values,
        "editing": editing,
    }


CHECK_LABELS = {
    AccountCheck.NET_POSITIVE: "Net positive",
    AccountCheck.NET_ZERO: "Net zero",
}


def balance_groupings(snapshot: Snapshot, balances: dict[str, float]) -> dict:
    """
    Composition of the balances of assets and liabilities, grouped by currency (base
    first) or by liquidity, listing every group that has accounts. Accounts keep the
    order they are defined in, and the internal ones are marked as such.
    """
    internal = {a.name for a in snapshot.accounts.values() if a.internal}

    def marked(rows: list[dict]) -> list[dict]:
        return [
            {**r, "internal": r["name"] in internal, **{k: marked(r[k]) for k in ("children", "rest") if r.get(k)}}
            for r in rows
        ]

    def grouped(acc_type: AccountType, key, groups: list) -> list[dict]:
        accs = [a for a in snapshot.accounts.values() if a.acc_type == acc_type]
        return [
            {**g, "children": marked(g["children"])}
            for g in grouped_composition({
                name: {a.name: balances.get(a.name, 0.0) for a in accs if key(a) == group}
                for group, name in groups
                if any(key(a) == group for a in accs)
            })
        ]

    by_currency = [(c, c) for c in snapshot.currencies]
    by_liquidity = list(LIQUIDITY_GROUPS.items())
    return {
        "currency": {
            "assets": grouped(AccountType.ASSET, lambda a: a.currency.name, by_currency),
            "liabilities": grouped(AccountType.LIABILITY, lambda a: a.currency.name, by_currency),
        },
        "liquidity": {
            "assets": grouped(AccountType.ASSET, lambda a: a.is_liquid, by_liquidity),
            "liabilities": grouped(AccountType.LIABILITY, lambda a: a.is_liquid, by_liquidity),
        },
    }


def over_time_charts(snapshot: Snapshot, month: str = "") -> dict:
    """
    Month-end charts up to `month` (all the ledger by default): liabilities over
    equity and net liquidity (liquid assets minus liquid liabilities) over equity.
    """
    monthly = month_end_balances(snapshot, month)
    debt = ratio_series(snapshot, monthly, lambda b, total: total(b, AccountType.LIABILITY))
    liquidity = ratio_series(
        snapshot, monthly,
        lambda b, total: total(b, AccountType.ASSET, True) - total(b, AccountType.LIABILITY, True),
    )
    return {
        "debt_ratio": line_chart(debt, percent),
        "debt_series": debt,
        "liquidity_ratio": line_chart(liquidity, percent),
        "liquidity_series": liquidity,
    }
