"""recurring.py: the recurring movements due and not in the ledger yet."""

from __future__ import annotations

from fastapi import APIRouter

from pledger.web.dependencies import StoreDep
from pledger.web.recurring import build_due

router = APIRouter(prefix="/api/recurring", tags=["recurring"])


@router.get("/due")
def list_due(store: StoreDep):
    """The recurring movements due (or upcoming) and not in the ledger CSV in their period, oldest first."""
    return build_due(store.settings.recurring_path, store.load())
