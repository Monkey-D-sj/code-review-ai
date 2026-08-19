
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Bumped whenever the schema or its meaning changes in a way that makes an
# older index.db incompatible; indexers check this before rebuilding.
INDEX_VERSION = 7

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
    parent_id INTEGER REFERENCES nodes(id),
    in_degree INTEGER NOT NULL DEFAULT 0,
    out_degree INTEGER NOT NULL DEFAULT 0,
    is_test INTEGER NOT NULL DEFAULT 0,
    decorators TEXT
);
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY,
    source TEXT,
    target TEXT,
    kind TEXT,
    file_path TEXT,
    resolution TEXT,
    origin TEXT,
    rule_id TEXT,
    confidence REAL,
    evidence_json TEXT,
    site_id TEXT
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
CREATE TABLE IF NOT EXISTS community_edges (
    community_id_a INTEGER NOT NULL,
    community_id_b INTEGER NOT NULL,
    weight INTEGER NOT NULL,
    PRIMARY KEY (community_id_a, community_id_b)
);
CREATE TABLE IF NOT EXISTS build_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    mtime REAL,
    size INTEGER,
    file_hash TEXT
);
CREATE TABLE IF NOT EXISTS tombstones (
    id INTEGER PRIMARY KEY,
    qname TEXT NOT NULL,
    kind TEXT NOT NULL,
    language TEXT,
    file_path TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    signature TEXT,
    is_test INTEGER NOT NULL DEFAULT 0,
    decorators TEXT,
    deleted_at_head TEXT,
    file_deleted INTEGER NOT NULL DEFAULT 0,
    upstream_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
CREATE INDEX IF NOT EXISTS idx_memberships_node ON flow_memberships(node_id);
CREATE INDEX IF NOT EXISTS idx_community_memberships_node ON community_memberships(node_id);
CREATE INDEX IF NOT EXISTS idx_tombstones_file ON tombstones(file_path);
CREATE INDEX IF NOT EXISTS idx_tombstones_qname ON tombstones(qname);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_nodes USING fts5(
    qualified_name, file_path, signature, decorators, end_line,
    content='nodes', content_rowid='id'
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # The MCP server shares one connection across threads (main, watcher, and
    # anyio's tool-call pool); writes are serialized by an app-level lock, so
    # disable SQLite's per-thread check. Also needed to make the existing
    # watcher-thread rebuilds legal, not just the 2.0 tool pool.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_nodes(conn)
    _migrate_edges(conn)


def _migrate_nodes(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema to pre-existing DBs.

    CREATE TABLE IF NOT EXISTS won't alter an existing table, so an older
    index.db needs ALTER TABLE to gain in_degree/out_degree, is_test, and
    decorators.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(nodes)")}
    if "in_degree" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN in_degree INTEGER NOT NULL DEFAULT 0")
    if "out_degree" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN out_degree INTEGER NOT NULL DEFAULT 0")
    if "is_test" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0")
    if "decorators" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN decorators TEXT")


def _migrate_edges(conn: sqlite3.Connection) -> None:
    """Drop the unused call_line column and add provenance columns to
    pre-existing DBs.

    CREATE TABLE IF NOT EXISTS won't alter an existing table, so an older
    index.db keeps call_line (nothing reads it anymore) and lacks the Phase 2
    provenance columns. Edge identity is (source, target, kind); the call site
    line number carried no topological meaning, while origin/rule_id/confidence/
    evidence_json/site_id carry the evidence the AI reviewer now consumes.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(edges)")}
    if "call_line" in cols:
        conn.execute("ALTER TABLE edges DROP COLUMN call_line")
    if "origin" not in cols:
        conn.execute("ALTER TABLE edges ADD COLUMN origin TEXT")
    if "rule_id" not in cols:
        conn.execute("ALTER TABLE edges ADD COLUMN rule_id TEXT")
    if "confidence" not in cols:
        conn.execute("ALTER TABLE edges ADD COLUMN confidence REAL")
    if "evidence_json" not in cols:
        conn.execute("ALTER TABLE edges ADD COLUMN evidence_json TEXT")
    if "site_id" not in cols:
        conn.execute("ALTER TABLE edges ADD COLUMN site_id TEXT")


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
