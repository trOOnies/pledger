"""summary.py"""

from __future__ import annotations

from fastapi import APIRouter

from pledger.web.dependencies import StoreDep
from pledger.web.views import build_accounts, build_view

router = APIRouter(prefix="/api/summary", tags=["summary"])


@router.get("")
def get_summary(store: StoreDep, month: str = "", account: str = "", initial: bool = False):
    """Results leave out the initial movements unless `initial`; balances include them."""
    snapshot = store.load()
    view = build_view(snapshot, month, account, initial)
    accounts = build_accounts(snapshot, month)
    return {
        "gains": view["gains"],
        "losses": view["losses"],
        "profit": view["profit"],
        "assets": accounts["assets"],
        "liabilities": accounts["liabilities"],
        "equity": accounts["equity"],
        "balances": {
            a["name"]: a["balance"]
            for accs in accounts["accounts_by_type"].values()
            for a in accs
        },
    }
