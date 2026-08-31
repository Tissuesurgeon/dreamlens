"""Watch DreamDEX fills in-process so DreamAgent can copy without Celery.

Celery Beat runs `full_event_sync_task` in production. `runserver` does not.
Without this loop, followed-trader fills never reach `detect_and_process_copy`,
so a RUNNING agent looks idle.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time

logger = logging.getLogger("dreamlens.copy.runtime")

_LOCK_FH = None
_POLL_THREAD = None
_SKIP_COMMANDS = {
    "migrate",
    "makemigrations",
    "collectstatic",
    "shell",
    "dbshell",
    "createsuperuser",
    "test",
    "flush",
    "check",
    "dumpdata",
    "loaddata",
}


def _django_command() -> str:
    args = [str(a) for a in sys.argv[1:] if not str(a).startswith("-")]
    return args[0] if args else ""


def should_listen() -> bool:
    if os.environ.get("COPY_DISABLE_POLL", "").lower() in {"1", "true", "yes"}:
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if "pytest" in sys.modules or os.path.basename(sys.argv[0]).startswith("pytest"):
        return False
    command = _django_command()
    if command in _SKIP_COMMANDS:
        return False
    if command == "runserver" and os.environ.get("RUN_MAIN") != "true":
        return False
    return True


def _acquire_poll_lock() -> bool:
    global _LOCK_FH
    path = os.path.join(tempfile.gettempdir(), "dreamlens-copy-poll.lock")
    handle = open(path, "w")
    try:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _LOCK_FH = handle
    return True


def run_copy_loop(*, interval: int | None = None) -> None:
    from django.conf import settings
    from django.db import connections

    from services.event_service import refresh_events_from_dreamdex
    from services.portfolio_service import auto_claim_settled_positions
    from services.trader_service import sync_traders_from_fills

    wait = interval or max(int(getattr(settings, "DREAMDEX_EVENT_SYNC_INTERVAL", 60) or 60), 15)
    logger.info("polling DreamDEX fills for autonomous copy every %ss", wait)
    while True:
        connections.close_all()
        try:
            refresh_events_from_dreamdex()
            stats = sync_traders_from_fills() or {}
            copies = stats.get("copy_executions") or 0
            created = stats.get("trades_created") or 0
            if copies or created:
                logger.info(
                    "copy poll trades_created=%s copy_executions=%s",
                    created,
                    copies,
                )
            claims = auto_claim_settled_positions()
            if claims.get("claimed"):
                logger.info("auto-claim claimed=%s", claims.get("claimed"))
        except Exception:
            logger.exception("autonomous copy poll failed")
        finally:
            connections.close_all()
        time.sleep(wait)


def start_copy_listener() -> None:
    if not should_listen():
        return
    global _POLL_THREAD
    if _POLL_THREAD and _POLL_THREAD.is_alive():
        return
    if not _acquire_poll_lock():
        logger.info("copy poller already running in another process")
        return
    _POLL_THREAD = threading.Thread(
        target=run_copy_loop,
        name="dreamlens-copy-poll",
        daemon=True,
    )
    _POLL_THREAD.start()
