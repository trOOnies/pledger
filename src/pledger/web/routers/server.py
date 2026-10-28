"""server.py: stopping pledger from the browser."""

from __future__ import annotations

import logging
import multiprocessing
import os
import signal

from fastapi import APIRouter, BackgroundTasks

from pledger.web.dependencies import StoreDep
from pledger.web.store import LedgerDataError

router = APIRouter(tags=["server"])


def stop_server() -> None:
    """
    Stop pledger like Ctrl+C would: its uvicorn reloader if it runs under one (as with
    `make serve`), which stops this process too, else this process itself.
    """
    parent = multiprocessing.parent_process()
    os.kill(parent.pid if parent else os.getpid(), signal.SIGINT)


@router.post("/quit")
def save_and_quit(store: StoreDep, background: BackgroundTasks):
    """
    Save what is left to save, the ids of the rows added to the CSVs of movements by
    hand (see LedgerStore.freeze_ids), then stop pledger once the response is sent.
    """
    try:
        store.freeze_ids()
    except (OSError, ValueError, LedgerDataError) as e:  # quit anyway, like on startup
        logging.getLogger(__name__).warning("Could not freeze the ids of the movements: %s", e)
    background.add_task(stop_server)
    return {"ok": True}
