"""dependencies.py"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from pledger.web.store import LedgerStore


def get_store(request: Request) -> LedgerStore:
    return request.app.state.store


StoreDep = Annotated[LedgerStore, Depends(get_store)]
