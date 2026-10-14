"""summary.py"""

from __future__ import annotations

from fastapi import APIRouter

from pledger.web.dependencies import StoreDep
from pledger.web.views import build_view

router = APIRouter(prefix="/api/summary", tags=["summary"])


@router.get("")
def get_summary(store: StoreDep, month: str = "", account: str = ""):
    view = build_view(store.load(), month, account)
    return {
        "gains": view["gains"],
        "losses": view["losses"],
        "profit": view["profit"],
        "balances": {
            a["name"]: a["balance"]
            for accs in view["accounts_by_type"].values()
            for a in accs
        },
    }
