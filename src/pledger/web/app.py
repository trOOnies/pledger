"""app.py"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from pledger.web.routers import (
    accounts,
    base_movements,
    forms,
    movements,
    pages,
    result_types,
    summary,
    work,
)
from pledger.web.store import LedgerDataError, LedgerStore, Settings
from pledger.web.templating import HERE, templates


async def ledger_data_error(request: Request, exc: LedgerDataError):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": str(exc)}, status_code=500)
    return templates.TemplateResponse(
        request, "error.html", {"error": str(exc), "settings": request.app.state.store.settings},
        status_code=500,
    )


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """At startup, add the companies of the work history to the debt accounts."""
    try:
        added = app.state.store.sync_work_creditors()
    except (LedgerDataError, ValueError) as e:
        logger.warning("Could not add the work companies to the debt accounts: %s", e)
    else:
        for acc, name in added:
            logger.info("Added the company %r to the debt account %r", name, acc)
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="pledger", lifespan=lifespan)
    app.state.store = LedgerStore(settings or Settings.from_env())
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.add_exception_handler(LedgerDataError, ledger_data_error)

    # HTML
    app.include_router(pages.router)
    app.include_router(forms.router)
    # API
    app.include_router(accounts.router)
    app.include_router(result_types.router)
    app.include_router(base_movements.router)
    app.include_router(movements.router)
    app.include_router(summary.router)
    app.include_router(work.router)
    return app


app = create_app()
