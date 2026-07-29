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
