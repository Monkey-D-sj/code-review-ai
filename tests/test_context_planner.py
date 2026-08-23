import json
from dataclasses import replace
from pathlib import Path

import pytest

from code_review_ai.config import load_config
from code_review_ai.context_planner import (
    evaluate_prepared_plans,
    plan_context,
)
from code_review_ai.db import connect, init_schema
from code_review_ai.full_agent_eval import FullAgentCase, PreparedCase


def _setup(tmp_path, monkeypatch, summary, files):
    root = tmp_path / "repo"
    root.mkdir()
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    cfg = load_config(str(root))
    cfg.repo_path = str(root)
    cfg.diff_base = "HEAD"
    conn = connect(str(tmp_path / "index.db"))
    init_schema(conn)
    monkeypatch.setattr(
        "code_review_ai.context_planner.build_change_summary",
        lambda config, connection, files=None: summary,
    )
    return cfg, conn


def _summary(records, uncovered=None, deleted=None):
    uncovered = uncovered or []
    deleted = deleted or []
    return {
        "summary": {
            "files_changed": len({r["file"] for r in records}),
            "lines_added": 1,
            "lines_removed": 1,
            "changed_functions": len(records),
            "uncovered_changes": len(uncovered),
            "delete_change": len(deleted),
        },
        "changed_functions": records,
        "uncovered_changes": uncovered,
        "delete_change": deleted,
    }


def _node(conn, qname, kind, file, start, end, is_test=0):
    conn.execute(
        "INSERT INTO nodes(qualified_name,kind,file_path,start_line,end_line,is_test) "
        "VALUES(?,?,?,?,?,?)",
        (qname, kind, str(file), start, end, is_test),
    )
    conn.commit()


def _diff(file, start, removed, added):
    return (
        f"diff --git a/{file} b/{file}\n"
        f"--- a/{file}\n+++ b/{file}\n"
        f"@@ -{start},1 +{start},1 @@\n-{removed}\n+{added}\n"
    )


def test_method_body_change_stays_local_despite_containing_class(
        tmp_path, monkeypatch):
    source = "class MapAdapter:\n    def read(self, value):\n        return value + 1\n"
    records = [
        {"qname": "mod::MapAdapter", "kind": "class", "file": "mod.py",
         "start_line": 1, "end_line": 3},
        {"qname": "mod::MapAdapter.read", "kind": "method", "file": "mod.py",
         "start_line": 2, "end_line": 3},
    ]
    cfg, conn = _setup(tmp_path, monkeypatch, _summary(records), {"mod.py": source})
    for record in records:
        _node(conn, record["qname"], record["kind"],
              Path(cfg.repo_path) / record["file"],
              record["start_line"], record["end_line"])

    plan = plan_context(
        cfg, conn, diff=_diff("mod.py", 3, "return value", "return value + 1"))

    assert plan["route"] == "local"
    assert plan["reasons"] == []
    assert plan["graph_stats"]["changed_symbols"] == 1
    assert len(json.dumps(plan, ensure_ascii=False)) <= 8_000


def test_class_scope_or_multiple_method_change_routes_to_graph(tmp_path, monkeypatch):
    source = "class Builder:\n    cache = {}\n    def one(self): pass\n    def two(self): pass\n"
    records = [
        {"qname": "mod::Builder", "kind": "class", "file": "mod.py",
         "start_line": 1, "end_line": 4},
        {"qname": "mod::Builder.one", "kind": "method", "file": "mod.py",
         "start_line": 3, "end_line": 3},
        {"qname": "mod::Builder.two", "kind": "method", "file": "mod.py",
         "start_line": 4, "end_line": 4},
    ]
    cfg, conn = _setup(tmp_path, monkeypatch, _summary(records), {"mod.py": source})
    for record in records:
        _node(conn, record["qname"], record["kind"],
              Path(cfg.repo_path) / "mod.py", record["start_line"], record["end_line"])
    diff = (
        "diff --git a/mod.py b/mod.py\n--- a/mod.py\n+++ b/mod.py\n"
        "@@ -2,1 +2,1 @@\n-cache = None\n+cache = {}\n"
        "@@ -3,2 +3,2 @@\n-def one(self): return 0\n+def one(self): pass\n"
        "-def two(self): return 0\n+def two(self): pass\n"
    )

    plan = plan_context(cfg, conn, diff=diff)

    assert plan["route"] == "graph"
    assert "class-scope-change" in plan["reasons"]
    assert "multiple-changed-symbols" in plan["reasons"]


def test_cross_file_production_caller_routes_to_graph_and_is_in_evidence(
        tmp_path, monkeypatch):
    records = [{"qname": "service::save", "kind": "function", "file": "service.py",
                "start_line": 1, "end_line": 2}]
    cfg, conn = _setup(tmp_path, monkeypatch, _summary(records), {
        "service.py": "def save(x):\n    return x\n",
        "api.py": "def endpoint():\n    return save(1)\n",
    })
    _node(conn, "service::save", "function", Path(cfg.repo_path) / "service.py", 1, 2)
    _node(conn, "api::endpoint", "function", Path(cfg.repo_path) / "api.py", 1, 2)
    conn.execute(
        "INSERT INTO edges(source,target,kind,resolution) VALUES(?,?,?,?)",
        ("api::endpoint", "service::save", "call", "resolved"),
    )
    conn.commit()

    plan = plan_context(
        cfg, conn, diff=_diff("service.py", 2, "return None", "return x"))

    assert plan["route"] == "graph"
    assert "cross-file-production-callers" in plan["reasons"]
    assert any(item.get("qname") == "api::endpoint" for item in plan["evidence"])


def test_test_selection_and_hard_budget_are_local_and_bounded(tmp_path, monkeypatch):
    long_body = "\n".join(f"    value_{i} = {i}" for i in range(300))
    source = f"class GraphAdapterBuilder:\n{long_body}\n"
    test_source = "class GraphAdapterBuilderTest:\n    def test_reuse(self):\n        pass\n"
    records = [{
        "qname": "graph::GraphAdapterBuilder", "kind": "class", "file": "graph.py",
        "start_line": 1, "end_line": 301,
    }]
    cfg, conn = _setup(tmp_path, monkeypatch, _summary(records), {
        "graph.py": source, "tests/test_graph_builder.py": test_source,
    })
    _node(conn, records[0]["qname"], "class", Path(cfg.repo_path) / "graph.py", 1, 301)
    _node(conn, "tests::GraphAdapterBuilderTest.test_reuse", "method",
          Path(cfg.repo_path) / "tests/test_graph_builder.py", 2, 3, is_test=1)
    diff = _diff("graph.py", 2, "value_0 = -1", "value_0 = 0") + ("x" * 10_000)

    plan = plan_context(cfg, conn, diff=diff, max_chars=1_600)

    rendered = json.dumps(plan, ensure_ascii=False)
    assert len(rendered) <= 1_600
    assert plan["metrics"]["truncated"] is True
    assert "tests/test_graph_builder.py" in plan["metrics"]["evidence_files"]


def test_offline_evaluation_reports_zero_llm_cost_and_unscored_route(tmp_path, monkeypatch):
    case = FullAgentCase(
        "case", "repo", "", "abc", ("mod.py",), "unused prompt", (), ())
    prepared = PreparedCase(case, str(tmp_path), "diff")
    setup = {"case": {"db_path": str(tmp_path / "db.sqlite"), "nodes": 1,
                      "edges": 0, "flows": 0, "elapsed_ms": 1.0}}
    fake_plan = {
        "route": "local", "reasons": [],
        "evidence": [{"file": "mod.py", "qname": "mod::f"}],
        "metrics": {"evidence_files": ["mod.py"], "serialized_chars": 123,
                    "truncated": False, "duplicate_file_entries": 0},
    }
    monkeypatch.setattr("code_review_ai.context_planner.plan_context",
                        lambda *args, **kwargs: fake_plan)
    # _case_config only needs this path to exist; connect creates the DB.
    report = evaluate_prepared_plans(
        [prepared], setup,
        [{"id": "case", "mutation_paths": ["mod.py"], "gold_files": ["test.py"]}],
    )

    assert report["llm_calls"] == 0
    assert report["model_cost_usd"] == 0.0
    assert report["aggregate"]["route_accuracy"] is None
    assert report["cases"][0]["mutation_file_recall"] == 1.0
