import json
import re
import sqlite3

from code_review_ai.db import init_schema
from code_review_ai.export_graph import export


def _seed(tmp_path) -> str:
    """Minimal DB: 4 communities. One structural cross-community edge (A import
    B), one call edge that also crosses communities (C calls F), and one
    intra-community call edge (C -> D)."""
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
            (5, "n::F", "function", "python", "n.py", 1, 5, "def F()"),
        ],
    )
    conn.executemany(
        "INSERT INTO edges(source, target, kind, file_path, resolution)"
        " VALUES(?,?,?,?,?)",
        [
            ("m::A", "m::B", "import", "m.py", "resolved"),
            ("m::C", "n::F", "call", "m.py", "resolved"),
            ("m::C", "m::D", "call", "m.py", "resolved"),
        ],
    )
    conn.executemany(
        "INSERT INTO communities(id, label, node_count, modularity) VALUES(?,?,?,?)",
        [(1, "comm_a", 1, 0.5), (2, "comm_b", 1, 0.5),
         (3, "comm_c", 2, 0.5), (4, "comm_d", 1, 0.5)],
    )
    conn.executemany(
        "INSERT INTO community_memberships(community_id, node_id) VALUES(?,?)",
        [(1, 1), (2, 2), (3, 3), (3, 4), (4, 5)],
    )
    # build would persist only structural cross-community edges here: the A->B
    # import. The C->F call edge crosses communities too but must NOT be in the
    # persisted community graph (call edges are excluded by community detection).
    conn.executemany(
        "INSERT INTO community_edges(community_id_a, community_id_b, weight)"
        " VALUES(?,?,?)",
        [(1, 2, 1)],
    )
    conn.commit()
    conn.close()
    return db


def test_export_communities_renders_persisted_edges(tmp_path):
    """The community view renders exactly the community_edges table build
    persisted - no re-derivation from `edges` (so no per-community recounting
    inflation and no call edges). The C->F call edge crosses communities but
    must not appear."""
    export(_seed(tmp_path), str(tmp_path / "g.html"), max_items=10,
           mode="communities")
    html = (tmp_path / "g.html").read_text(encoding="utf-8")
    data = json.loads(re.search(r"const data = (\{.*?\});", html, re.S).group(1))
    assert data["edges"] == [{"source": 1, "target": 2, "weight": 1}]
