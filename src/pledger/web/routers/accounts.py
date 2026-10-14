"""accounts.py"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pledger.web.dependencies import StoreDep
from pledger.web.schemas import AccountIn

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("")
def list_accounts(store: StoreDep):
    return [
        {"name": a.name, "description": a.description, "type": a.acc_type.value}
        for a in store.load().accounts.values()
    ]


@router.post("", status_code=201)
def create_account(store: StoreDep, account: AccountIn):
    try:
        store.add_account(account.name, account.type.value, account.description)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True}
