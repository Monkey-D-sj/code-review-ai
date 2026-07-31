import json
import re
import sqlite3

from code_review_ai.db import init_schema
from code_review_ai.export_graph import export


def _seed(tmp_path) -> str:
    """Minimal DB: 3 communities, one cross-community resolved edge (A->B) and
    one intra-community edge (C->D)."""
    db = str(tmp_path / "g.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.executemany(
        "INSERT INTO nodes(id, qualified_name, kind, language, file_path,"
        " start_line, end_line, signature) VALUES(?,?,?,?,?,?,?,?)",
        [
            (1, "m::A", "function", "python", "m.py", 1, 5, "def A()"),
            (2, "m::B", "function", "python", "m.py", 6, 10, "def B()"),
            (3, "m::C", "function", "python", "m.py", 11, 15, "def C()"),
            (4, "m::D", "function", "python", "m.py", 16, 20, "def D()"),
        ],
    )
    conn.executemany(
        "INSERT INTO edges(source, target, kind, file_path, call_line, resolution)"
        " VALUES(?,?,?,?,?,?)",
        [
            ("m::A", "m::B", "call", "m.py", 4, "resolved"),
            ("m::C", "m::D", "call", "m.py", 14, "resolved"),
        ],
    )
    conn.executemany(
        "INSERT INTO communities(id, label, node_count, modularity) VALUES(?,?,?,?)",
        [(1, "comm_a", 1, 0.5), (2, "comm_b", 1, 0.5), (3, "comm_c", 2, 0.5)],
    )
    conn.executemany(
        "INSERT INTO community_memberships(community_id, node_id) VALUES(?,?)",
        [(1, 1), (2, 2), (3, 3), (3, 4)],
    )
    conn.commit()
    conn.close()
    return db


def test_export_communities_counts_cross_edge_once(tmp_path):
    """Inter-community edges must be counted once per underlying edge, not once
    per community. The old per-community loop inflated the A->B edge to weight 3
    (three communities) and even included C->D-style noise; both must be exact."""
    export(_seed(tmp_path), str(tmp_path / "g.html"), max_items=10,
           mode="communities")
    html = (tmp_path / "g.html").read_text(encoding="utf-8")
    data = json.loads(re.search(r"const data = (\{.*?\});", html, re.S).group(1))
    # only the A->B edge crosses communities; C->D stays inside community 3.
    assert data["edges"] == [{"source": 1, "target": 2, "weight": 1}]
