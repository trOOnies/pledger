"""accounts.py"""

from __future__ import annotations

from fastapi import APIRouter

from pledger.web.dependencies import StoreDep

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("")
def list_accounts(store: StoreDep):
    return [
        {"name": a.name, "description": a.description, "type": a.acc_type.value}
        for a in store.load().accounts.values()
    ]
