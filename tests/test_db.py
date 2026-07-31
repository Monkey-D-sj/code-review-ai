from conftest import Q
import sqlite3
from code_review_ai.db import connect, init_schema


def test_init_schema_creates_tables(tmp_path):
    conn = connect(str(tmp_path / "x.db"))
    init_schema(conn)
    names = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"nodes", "edges", "flows", "flow_memberships",
            "communities", "community_memberships"} <= names
    # WAL enabled
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_init_schema_is_idempotent(tmp_path):
    conn = connect(str(tmp_path / "x.db"))
    init_schema(conn)
    init_schema(conn)  # must not raise


def test_init_schema_migrates_legacy_nodes(tmp_path):
    """An index.db from before in_degree/out_degree existed gets the columns
    added via ALTER TABLE (CREATE TABLE IF NOT EXISTS won't touch it)."""
    db = tmp_path / "legacy.db"
    # build a DB with the old nodes schema (no degree columns) and a row
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE nodes ("
        "id INTEGER PRIMARY KEY, qualified_name TEXT UNIQUE, kind TEXT,"
        "language TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        "signature TEXT, parent_id INTEGER REFERENCES nodes(id));"
    )
    conn.execute("INSERT INTO nodes(qualified_name) VALUES('mod::old')")
    conn.commit()
    conn.close()

    conn = connect(str(db))
    init_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
    assert "in_degree" in cols and "out_degree" in cols
    # legacy row backfilled with the column default
    row = conn.execute(
        "SELECT in_degree, out_degree FROM nodes WHERE qualified_name='mod::old'"
    ).fetchone()
    assert row["in_degree"] == 0 and row["out_degree"] == 0
