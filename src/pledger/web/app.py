"""app.py"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from pledger.web.routers import (
    accounts,
    base_movements,
    conversions,
    credit_cards,
    debt,
    forms,
    investments,
    movements,
    pages,
    result_types,
    server,
    services,
    summary,
    work,
)
from pledger.web.store import LedgerDataError, LedgerStore, Settings
from pledger.web.templating import HERE, templates


async def content_language(request: Request, call_next):
    """Every response is in English, whatever the language of the names and comments in the data."""
    response = await call_next(request)
    response.headers.setdefault("Content-Language", "en")
    return response


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _forbidden(request: Request, detail: str):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": detail}, status_code=403)
    return PlainTextResponse(detail, status_code=403)


async def same_site_only(request: Request, call_next):
    """
    Keep other sites out, since the app has no authentication: refuse a Host header that isn't
    one of the allowed hosts (DNS rebinding), and a change sent by a page of another origin (a
    form of a malicious site posting to 127.0.0.1). Requests without an Origin or Referer, like
    those of curl or scripts, are let through.
    """
    host = request.headers.get("host", "")
    allowed = request.app.state.store.settings.allowed_hosts
    if allowed is not None and (urlsplit(f"//{host}").hostname or "") not in allowed:
        return _forbidden(request, f"Host not allowed: {host}")
    if request.method not in SAFE_METHODS:
        source = request.headers.get("origin") or request.headers.get("referer")
        if source and urlsplit(source).netloc.lower() != host.lower():
            return _forbidden(request, "Cross-origin request refused")
    return await call_next(request)


async def ledger_data_error(request: Request, exc: LedgerDataError):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": str(exc)}, status_code=500)
    return templates.TemplateResponse(
        request, "error.html", {"error": str(exc), "settings": request.app.state.store.settings},
        status_code=500,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Freeze the ids of the rows added to the CSVs of movements while pledger was off.
    try:
        app.state.store.freeze_ids()
    except (OSError, ValueError, LedgerDataError) as e:  # the pages show what is wrong
        logging.getLogger(__name__).warning("Could not freeze the ids of the movements: %s", e)
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="pledger", lifespan=lifespan)
    app.state.store = LedgerStore(settings or Settings.from_env())
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.add_exception_handler(LedgerDataError, ledger_data_error)
    app.middleware("http")(content_language)
    app.middleware("http")(same_site_only)

    # HTML
    app.include_router(pages.router)
    app.include_router(forms.router)
    # API
    app.include_router(accounts.router)
    app.include_router(result_types.router)
    app.include_router(base_movements.router)
    app.include_router(movements.router)
    app.include_router(investments.router)
    app.include_router(conversions.router)
    app.include_router(debt.router)
    app.include_router(services.router)
    app.include_router(credit_cards.router)
    app.include_router(summary.router)
    app.include_router(work.router)
    app.include_router(server.router)
    return app


app = create_app()
