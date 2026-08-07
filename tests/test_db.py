from conftest import Q
import sqlite3
from code_review_ai.db import INDEX_VERSION, connect, init_schema


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
    assert "in_degree" in cols and "out_degree" in cols and "is_test" in cols
    # legacy row backfilled with the column default
    row = conn.execute(
        "SELECT in_degree, out_degree, is_test FROM nodes WHERE qualified_name='mod::old'"
    ).fetchone()
    assert row["in_degree"] == 0 and row["out_degree"] == 0 and row["is_test"] == 0


def test_files_table_and_busy_timeout(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    # files 表存在且可写
    conn.execute(
        "INSERT INTO files(path,mtime,size,file_hash) VALUES('a.py', 1.0, 3, 'x')")
    row = conn.execute("SELECT * FROM files").fetchone()
    assert row["path"] == "a.py" and row["size"] == 3
    assert INDEX_VERSION == 5
    # busy_timeout 生效（PRAGMA 返回毫秒）
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    conn.close()


def test_init_schema_migrates_decorators_column(tmp_path):
    """An index.db from before decorators existed gains the column via ALTER
    TABLE (CREATE TABLE IF NOT EXISTS won't touch it)."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE nodes ("
        "id INTEGER PRIMARY KEY, qualified_name TEXT UNIQUE, kind TEXT,"
        "language TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        "signature TEXT, parent_id INTEGER REFERENCES nodes(id),"
        "in_degree INTEGER NOT NULL DEFAULT 0,"
        "out_degree INTEGER NOT NULL DEFAULT 0,"
        "is_test INTEGER NOT NULL DEFAULT 0);"
    )
    conn.execute("INSERT INTO nodes(qualified_name) VALUES('mod::old')")
    conn.commit()
    conn.close()

    conn = connect(str(db))
    init_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
    assert "decorators" in cols
    row = conn.execute(
        "SELECT decorators FROM nodes WHERE qualified_name='mod::old'"
    ).fetchone()
    assert row["decorators"] is None


def test_init_schema_creates_tombstones_table(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tombstones)")}
    assert {"qname", "kind", "file_path", "start_line", "end_line",
            "signature", "is_test", "decorators", "deleted_at_head",
            "file_deleted", "upstream_json"} <= cols
    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 0
    init_schema(conn)  # 幂等
