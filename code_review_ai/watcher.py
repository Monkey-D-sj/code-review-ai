
import logging
import sqlite3
import threading
from contextlib import nullcontext

from code_review_ai.config import Config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import ParseCache, is_stale, rebuild
from code_review_ai.parser import SOURCE_SUFFIXES

log = logging.getLogger(__name__)


def startup_rebuild(config: Config, conn: sqlite3.Connection,
                    cache: ParseCache | None = None,
                    lock: threading.Lock | None = None) -> bool:
    """Rebuild if index missing or stale. Returns True if rebuilt."""
    if is_stale(config, conn):
        log.info("index stale/missing; rebuilding")
        with (lock or nullcontext()):
            rebuild(config, conn, cache)
        return True
    return False


def run_watcher(config: Config, cache: ParseCache | None,
                lock: threading.Lock | None,
                stop_event: threading.Event | None = None) -> None:
    """Watch .py files; debounce; rebuild on change. Blocks until stop_event set.

    Uses its own DB connection created here, in this thread, so it never shares
    a connection with the server/query thread (sqlite3 connections are not
    cross-thread safe by default). Rebuilds are serialized with `lock` against
    the rebuild_index tool and share `cache` across rebuilds."""
    from watchfiles import watch
    conn = connect(config.db_path)
    init_schema(conn)
    stop_event = stop_event or threading.Event()
    debounce = max(config.watch_debounce_ms, 50)
    try:
        for changes in watch(config.repo_path, debounce=debounce,
                             watch_filter=_source_file, stop_event=stop_event):
            if stop_event.is_set():
                break
            log.info("detected %d changes; rebuilding", len(changes))
            try:
                with (lock or nullcontext()):
                    rebuild(config, conn, cache)
            except Exception:  # never let watcher die on rebuild error
                log.exception("rebuild failed; keeping old index")
    except Exception:
        log.exception("watcher stopped unexpectedly")
        return


def _source_file(change, path):
    import os
    return path.endswith(SOURCE_SUFFIXES) and os.path.isfile(path)
