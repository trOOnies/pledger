"""audit.py: the Audit tab: the balances checked by hand against the actual ones."""

from __future__ import annotations

from pledger.enums import AccountType
from pledger.models import InvestmentAccount, is_foreign
from pledger.web.store import Snapshot, _parse_number

AUDITED_TYPES = [AccountType.ASSET, AccountType.LIABILITY]


def _audited_accounts(snapshot: Snapshot) -> list:
    """The asset and liability accounts, by type: the equity is what they add up to."""
    return [a for t in AUDITED_TYPES for a in snapshot.accounts.values() if a.acc_type == t]


def _shown_in(snapshot: Snapshot, account) -> tuple[str, int]:
    """The currency of a balance of an account (see Snapshot.balances_today) and its decimals."""
    if is_foreign(account):
        return account.currency.name, 4  # with the decimals of the amounts in other currencies
    return snapshot.base_currency.name, 2


def audit_form(snapshot: Snapshot, values: dict[str, str] | None = None) -> dict:
    """The form to fill in the balances: blind, so it must never show the actual ones."""
    fields = [
        {
            "name": a.name, "description": a.description, "type": a.acc_type.value,
            "currency": _shown_in(snapshot, a)[0], "decimals": _shown_in(snapshot, a)[1],
        }
        for a in _audited_accounts(snapshot)
    ]
    return {"fields": fields, "values": values or {}, "results": None}


def audit_results(snapshot: Snapshot, values: dict[str, str]) -> dict:
    """
    The balances filled in next to the actual ones, today. A balance left empty isn't
    audited, while a 0 is audited as 0. Each difference has the correction that would fix
    it (see Snapshot.correction_of), but for the investment accounts, whose balance comes
    from their investments. Raises ValueError if a balance is invalid or none is filled in.
    """
    audited = {
        a.name: _parse_number(values[a.name], f"balance of {a.name}")
        for a in _audited_accounts(snapshot) if values.get(a.name, "").strip()
    }
    if not audited:
        raise ValueError("Fill in at least one balance: the ones left empty aren't audited")
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
