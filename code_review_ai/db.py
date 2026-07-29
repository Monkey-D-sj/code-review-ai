
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY,
    qualified_name TEXT UNIQUE,
    kind TEXT,
    language TEXT,
    file_path TEXT,
    start_line INTEGER,
    end_line INTEGER,
    signature TEXT,
    parent_id INTEGER REFERENCES nodes(id)
);
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY,
    source TEXT,
    target TEXT,
    kind TEXT,
    file_path TEXT,
    call_line INTEGER,
    resolution TEXT
);
CREATE TABLE IF NOT EXISTS flows (
    id INTEGER PRIMARY KEY,
    name TEXT,
    entry_point_id INTEGER,
    depth INTEGER,
    node_count INTEGER,
    file_count INTEGER,
    criticality REAL,
    path_json TEXT
);
CREATE TABLE IF NOT EXISTS flow_memberships (
    flow_id INTEGER,
    node_id INTEGER,
    position INTEGER,
    PRIMARY KEY (flow_id, node_id)
);
CREATE TABLE IF NOT EXISTS communities (
    id INTEGER PRIMARY KEY,
    label TEXT,
    node_count INTEGER,
    modularity REAL
);
CREATE TABLE IF NOT EXISTS community_memberships (
    community_id INTEGER,
    node_id INTEGER,
    PRIMARY KEY (community_id, node_id)
);
CREATE TABLE IF NOT EXISTS build_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
CREATE INDEX IF NOT EXISTS idx_memberships_node ON flow_memberships(node_id);
CREATE INDEX IF NOT EXISTS idx_community_memberships_node ON community_memberships(node_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Atomic transaction; rolls back on exception."""
    conn.execute("BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
