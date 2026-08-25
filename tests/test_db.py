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
    assert INDEX_VERSION == 8
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


def test_init_schema_migrates_legacy_edges(tmp_path):
    """An index.db from before the Phase 2 provenance columns gains them via
    ALTER TABLE (CREATE TABLE IF NOT EXISTS won't touch it)."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE edges ("
        "id INTEGER PRIMARY KEY, source TEXT, target TEXT, kind TEXT,"
        "file_path TEXT, resolution TEXT);"
    )
    conn.execute("INSERT INTO edges(source,target,kind,file_path,resolution)"
                 " VALUES('mod::a','mod::b','call','app.py','resolved')")
    conn.commit()
    conn.close()

    conn = connect(str(db))
    init_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(edges)")}
    assert {"origin", "rule_id", "confidence", "evidence_json", "site_id"} <= cols
    # the legacy row keeps its identity; new columns default to NULL
    row = conn.execute(
        "SELECT resolution, origin, rule_id, confidence, evidence_json, site_id"
        " FROM edges").fetchone()
    assert row["resolution"] == "resolved"
    assert row["origin"] is None and row["rule_id"] is None
    assert row["confidence"] is None and row["site_id"] is None
    assert row["evidence_json"] is None


def test_init_schema_creates_tombstones_table(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tombstones)")}
    assert {"qname", "kind", "file_path", "start_line", "end_line",
            "signature", "is_test", "decorators", "deleted_at_head",
            "file_deleted", "upstream_json"} <= cols
    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 0
    init_schema(conn)  # 幂等


def test_init_schema_creates_fts_nodes(tmp_path):
    conn = connect(str(tmp_path / "fts.db"))
    init_schema(conn)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fts_nodes'"
    ).fetchone()
    assert row is not None
    conn.close()


def test_init_schema_enables_auto_vacuum_full(tmp_path):
    """A freshly initialized index gets auto_vacuum=FULL so repeated rebuilds
    reclaim free pages instead of bloating the file (the freelist can grow to
    80% of file size after many delete-all rebuilds)."""
    conn = connect(str(tmp_path / "av.db"))
    init_schema(conn)
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 1  # 1 = FULL
    init_schema(conn)  # idempotent: no re-VACUUM, mode stays FULL
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 1
    conn.close()


def test_init_schema_converts_bloated_db_and_reclaims_free_pages(tmp_path):
    """An index created before auto_vacuum (freelist full of deleted pages) is
    converted on init_schema: mode flips to FULL and free pages are truncated
    back to the OS."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE t(x TEXT);")
    with conn:
        conn.executemany(
            "INSERT INTO t VALUES (?)", [(str(i) * 50,) for i in range(8000)])
    conn.execute("DELETE FROM t")
    conn.commit()
    freelist_before = conn.execute("PRAGMA freelist_count").fetchone()[0]
    conn.close()
    assert freelist_before > 0

    conn = connect(str(db))
    init_schema(conn)
    mode = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
    freelist_after = conn.execute("PRAGMA freelist_count").fetchone()[0]
    conn.close()
    assert mode == 1
    assert freelist_after < freelist_before
