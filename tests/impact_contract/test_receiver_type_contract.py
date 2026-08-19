"""Impact contract — receiver declared-type binding (Slice 1, PY-M12).

End-to-end: the receiver-bound edge `go -> widget::Widget.run` must appear in
impact output (downstream of go, upstream of Widget.run), and an incremental
sync must produce the same index as a fresh full rebuild.
"""

from code_review_ai.impact import get_impact
from code_review_ai.qname import join as Q

from helpers import assert_incremental_equals_rebuild, build_index, qname_set


GRAPH = {
    "widget.py": "class Widget:\n    def run(self):\n        return 1\n",
    "svc.py": (
        "from widget import Widget\n"
        "\n"
        "def go(w: Widget):\n"
        "    return w.run()\n"
    ),
    "app.py": (
        "from svc import go\n"
        "from widget import Widget\n"
        "\n"
        "def main():\n"
        "    return go(Widget())\n"
    ),
}


def test_receiver_bound_call_in_downstream(tmp_path):
    cfg, conn = build_index(tmp_path, GRAPH)
    impact = get_impact(conn, [Q("svc", "go")])[0]
    assert Q("widget", "Widget.run") in qname_set(impact["downstream"])


def test_receiver_bound_call_in_upstream(tmp_path):
    cfg, conn = build_index(tmp_path, GRAPH)
    impact = get_impact(conn, [Q("widget", "Widget.run")])[0]
    assert Q("svc", "go") in qname_set(impact["upstream"])


def test_receiver_typing_incremental_equals_rebuild(tmp_path):
    cfg, conn = build_index(tmp_path, GRAPH)

    def apply_changes(repo_path):
        (repo_path / "svc.py").write_text(
            "from widget import Widget\n"
            "\n"
            "def go(w: Widget):\n"
            "    return w.run()\n"
            "\n"
            "def go2(w: Widget):\n"
            "    return w.run()\n",
            encoding="utf-8")

    assert_incremental_equals_rebuild(
        cfg, conn, apply_changes, [Q("svc", "go"), Q("svc", "go2")])
