"""audit.py: the Audit tab: the balances checked by hand against the actual ones."""

from __future__ import annotations

from pledger.enums import AccountCheck, AccountType
from pledger.models import Audit, AuditEntry, InvestmentAccount, is_foreign
from pledger.web.store import LEDGER_ID, Snapshot, _parse_number, stored_ids
from pledger.web.views.common import ACCOUNT_GROUPS, account_group, ledger_link

# The groups of the asset accounts: on the left of the audit form when wide enough, the rest on the right
ASSET_GROUPS = {"assets", "foreign_assets", "investments"}

AUDITED_TYPES = [AccountType.ASSET, AccountType.LIABILITY]


def _audited_accounts(snapshot: Snapshot) -> list:
    """The asset and liability accounts, by type: the equity is what they add up to."""
    return [a for t in AUDITED_TYPES for a in snapshot.accounts.values() if a.acc_type == t]


def _fixed_at_zero(account) -> bool:
    """Whether the balance of an account is always 0.00 (net zero, like the INTERMEDIARY): never filled in."""
    return AccountCheck.NET_ZERO in account.checks


def _shown_in(snapshot: Snapshot, account) -> tuple[str, int]:
    """The currency of a balance of an account (see Snapshot.balances_today) and its decimals."""
    if is_foreign(account):
        return account.currency.name, 4  # with the decimals of the amounts in other currencies
    return snapshot.base_currency.name, 2


def audit_form(snapshot: Snapshot, values: dict[str, str] | None = None) -> dict:
    """
    The form to fill in the balances: blind, so it must never show the actual ones. Its
    fields are in the groups of ACCOUNT_GROUPS that have any (one form all the same), each
    by the currency of its balance (the base one first, then the others by name) and then
    by account name. The net zero accounts are fixed at 0.00, and the net positive ones can't be negative.
    """
    fields = [
        {
            "name": a.name, "description": a.description, "type": a.acc_type.value,
            "currency": _shown_in(snapshot, a)[0], "decimals": _shown_in(snapshot, a)[1],
            "group": account_group(a), "fixed": _fixed_at_zero(a),
            "non_negative": AccountCheck.NET_POSITIVE in a.checks,
        }
        for a in _audited_accounts(snapshot)
    ]
    base = snapshot.base_currency.name
    fields.sort(key=lambda f: (f["currency"] != base, f["currency"], f["name"].casefold(), f["name"]))
    groups = [
        {
            "key": key, "emoji": emoji, "label": label, "fields": [f for f in fields if f["group"] == key],
            "side": key in ASSET_GROUPS,
        }
        for key, (emoji, label) in ACCOUNT_GROUPS.items()
    ]
    return {"groups": [g for g in groups if g["fields"]], "values": values or {}, "results": None}


def audit_results(snapshot: Snapshot, values: dict[str, str]) -> dict:
    """
    The balances filled in next to the actual ones, today. A balance left empty isn't
    audited, while a 0 is audited as 0. Each difference has the correction that would fix
    it (see Snapshot.correction_of), but for the investment accounts, whose balance comes
    from their investments. The net zero accounts are always audited at 0.00, whatever is
    filled in. Raises ValueError if a balance is invalid, a net positive one is negative, or
    none is filled in (but those fixed at 0.00).
    """
    audited = {
        a.name: _parse_number(values[a.name], f"balance of {a.name}")
        for a in _audited_accounts(snapshot) if not _fixed_at_zero(a) and values.get(a.name, "").strip()
    }
    if not audited:
        raise ValueError("Fill in at least one balance: the ones left empty aren't audited")
    negative = [
        a.name for a in _audited_accounts(snapshot)
        if AccountCheck.NET_POSITIVE in a.checks and a.name in audited and round(audited[a.name], 4) < 0
    ]
    if negative:
        raise ValueError(f"The balance of a net positive account can't be negative: {', '.join(negative)}")
    audited |= {a.name: 0.0 for a in _audited_accounts(snapshot) if _fixed_at_zero(a)}
    values = {**values, **{a.name: "0.00" for a in _audited_accounts(snapshot) if _fixed_at_zero(a)}}
    balances = snapshot.balances_today()
    results = []
    for a in _audited_accounts(snapshot):
        if a.name not in audited:
            continue
        currency, decimals = _shown_in(snapshot, a)
        difference = audited[a.name] - balances[a.name]
        correction = snapshot.correction_of(a, difference)
        results.append({
            "name": a.name, "type": a.acc_type.value, "currency": currency, "decimals": decimals,
            "inputted": audited[a.name], "actual": balances[a.name],
            "difference": difference if correction else 0.0,
            "matches": correction is None,
            "investment": isinstance(a, InvestmentAccount),
            "base": correction[0] if correction else None,
            "amount": correction[1] if correction else None,
        })
    return {
        "results": results,
        "values": {name: values[name] for name in audited},
        "differences": sum(not r["matches"] for r in results),
        "skipped": len(_audited_accounts(snapshot)) - len(results),
    }


def audit_entries(results: list[dict]) -> list[AuditEntry]:
    """The entries of an audit, to save, from its results (see audit_results)."""
    return [AuditEntry(r["name"], r["currency"], r["inputted"], r["actual"], r["difference"]) for r in results]


def audit_history(snapshot: Snapshot, audits: list[Audit]) -> list[dict]:
    """
    The audits made, newest first, with the amounts of each account as they were then.
    A correction links to its movement, unless it was deleted from the ledger.
    """
    dates = dict(zip(stored_ids(snapshot.rows, LEDGER_ID), snapshot.rows["move_date"]))
    base = snapshot.base_currency.name

    def entry(e: AuditEntry) -> dict:
        return {
            "account": e.account, "currency": e.currency, "decimals": 2 if e.currency == base else 4,
            "inputted": e.inputted, "actual": e.actual, "difference": e.difference, "matches": e.matches,
            "corrected": e.corrected, "base": e.base_movement, "amount": e.amount, "movement_id": e.movement_id,
            "link": ledger_link(e.movement_id, dates[e.movement_id]) if e.movement_id and e.movement_id in dates else None,
        }

    return [
        {
            "id": a.id, "date": a.date.strftime("%Y-%m-%d %H:%M"), "audited": len(a.entries),
            "differences": a.differences, "corrected": a.corrected, "entries": [entry(e) for e in a.entries],
        }
        for a in reversed(audits)
    ]
