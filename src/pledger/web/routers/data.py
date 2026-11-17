"""data.py: the Data tab, exporting the data files as a zip and importing one back, and its API."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response

from pledger.web.dependencies import StoreDep
from pledger.web.store import ImportResult, LedgerDataError, LedgerStore
from pledger.web.store.data_store import MAX_TOTAL_SIZE
from pledger.web.templating import is_partial, templates

router = APIRouter(include_in_schema=False)
api = APIRouter(prefix="/api/data", tags=["data"])


def data_page(request: Request, store: LedgerStore, **extra):
    """The Data tab, or only its content for HTMX. It never loads the ledger: it must work when the data doesn't."""
    context = {
        "files": store.data_files(), "backups": store.backups(),
        "backups_folder": store.backups_folder, **extra,
    }
    return templates.TemplateResponse(request, "_data.html" if is_partial(request) else "data.html", context)


def export_response(store: LedgerStore) -> Response:
    return Response(
        store.export_zip(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="pledger-{dt.date.today().isoformat()}.zip"'},
    )


def read_upload(file: UploadFile) -> bytes:
    """The bytes of the zip uploaded, refusing one bigger than any export could be."""
    data = file.file.read(MAX_TOTAL_SIZE + 1)
    if len(data) > MAX_TOTAL_SIZE:
        raise ValueError(f"The file is too big: more than {MAX_TOTAL_SIZE // 1024 // 1024} MB")
    return data


def import_upload(store: LedgerStore, file: UploadFile) -> ImportResult:
    """Import the zip uploaded, then do what pledger does on startup with the new files (see app.lifespan)."""
    result = store.import_zip(read_upload(file))
    for run, what in ((store.freeze_ids, "freeze the ids of the movements"), (store.fill_estimated, "fill in the estimates")):
        try:
            run()
        except (OSError, ValueError, LedgerDataError) as e:  # the pages show what is wrong
            logging.getLogger(__name__).warning("Could not %s after the import: %s", what, e)
    return result


@router.get("/data", response_class=HTMLResponse)
def data(request: Request, store: StoreDep):
    return data_page(request, store)


@router.get("/data/export")
def export(store: StoreDep):
    """Every data file, in a zip named after today."""
    return export_response(store)


@router.get("/data/files/{name:path}")
def data_file(store: StoreDep, name: str):
    """One data file, by its name in an export (e.g. movements/ledger.csv)."""
    path = store.data_paths().get(name)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail=f"No data file {name!r}")
    return FileResponse(path, filename=path.name)


@router.get("/data/backups/{name}")
def backup(store: StoreDep, name: str):
    """One of the backups made before an import."""
    path = store.backup_path(name)
    if path is None:
        raise HTTPException(status_code=404, detail=f"No backup {name!r}")
    return FileResponse(path, filename=name, media_type="application/zip")


@router.post("/data/import", response_class=HTMLResponse)
def import_data(
    request: Request, store: StoreDep, file: Annotated[UploadFile, File()],
    confirm: Annotated[bool, Form()] = False,
):
    if not confirm:
        return data_page(request, store, import_error="Check that the data of the zip replaces yours to import it")
    try:
        result = import_upload(store, file)
    except ValueError as e:
        return data_page(request, store, import_error=str(e))
    return data_page(request, store, result=result)


@api.get("/export", response_class=Response, responses={200: {"content": {"application/zip": {}}}})
def api_export(store: StoreDep):
    """Every data file, in a zip: each one at its path in info/ (e.g. movements/ledger.csv)."""
    return export_response(store)


@api.post("/import")
def api_import(store: StoreDep, file: Annotated[UploadFile, File()]):
    """
    Replace every data file with those of a zip exported before, once they load. The files missing
    from the zip are removed, and the current ones are saved to a backup first.
    """
    try:
        result = import_upload(store, file)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {
        "imported": result.imported, "removed": result.removed, "ignored": result.ignored,
        "backup": str(result.backup),
    }
