import logging
import os
import sqlite3
import threading
from contextlib import nullcontext

from code_review_ai.config import Config
from code_review_ai.db import connect, init_schema
from code_review_ai.parser import SOURCE_SUFFIXES
from code_review_ai.update import sync, update_nodes_edges

from watchfiles import Change  # watchfiles is a hard dependency; module-level for _source_file

log = logging.getLogger(__name__)


def startup_sync(config: Config, conn: sqlite3.Connection,
                 lock: threading.Lock | None = None) -> bool:
    """Bring the index current at startup. Returns True if anything was updated."""
    with (lock or nullcontext()):
        result = sync(config, conn)
    if result.get("full_rebuild"):
        return True
    return bool(result["nodes"] or result["edges"] or result["flows"]
                or result["communities"])


def run_watcher(config: Config, lock: threading.Lock | None,
                stop_event: threading.Event | None = None) -> None:
    """Watch source files; on change, update nodes/edges incrementally.
    Blocks until stop_event set. Uses its own DB connection."""
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
            paths = _relative_paths(config, changes)
            log.info("detected %d changes; updating nodes/edges", len(paths))
            try:
                with (lock or nullcontext()):
                    update_nodes_edges(config, conn, paths)
            except Exception:
                log.exception("update failed; keeping old index")
    except Exception:
        log.exception("watcher stopped unexpectedly")
        return


def _relative_paths(config: Config, changes) -> list[str]:
    out = []
    for _change, path in changes:
        rel = os.path.relpath(path, config.repo_path).replace("\\", "/")
        out.append(rel)
    return out


def _source_file(change, path):
    if not path.endswith(SOURCE_SUFFIXES):
        return False
    if change == Change.deleted:
        return True                      # deletes must reach the updater
    return os.path.isfile(path)
