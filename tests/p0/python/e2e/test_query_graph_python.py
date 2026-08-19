from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from code_review_ai.config import load_config
from code_review_ai.mcp_server import create_server


ROOT = Path(__file__).resolve().parents[4]
FIXTURE_REPO = ROOT / "tests" / "p0" / "python" / "fixture_repo"
CASE_DIR = ROOT / "tests" / "p0" / "python" / "cases" / "query"
CASE_FILES = tuple(sorted(CASE_DIR.glob("*.json")))
COVERAGE_FILE = ROOT / "tests" / "p0" / "python" / "p0-python-coverage.json"
METRICS_FILE = ROOT / "tests" / "p0" / "python" / "p0-python-metrics.json"

REQUIRED_CASES = {
    "py_call_top_level_and_control_flow",
    "py_call_scope_and_recursion",
    "py_call_methods_and_constructor",
    "py_call_cross_module",
    "py_contains_edges",
    "py_import_edges",
    "py_extends_edges",
    "py_all_edges",
    "py_query_contract",
    "py_nonresolved_call_edges",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=python-p0@example.test",
            "-c",
            "user.name=python-p0",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def query_tools(tmp_path_factory: pytest.TempPathFactory):
    """Build one isolated index and reuse it for every public query case."""
    work = tmp_path_factory.mktemp("python-p0")
    repo = work / "fixture_repo"
    shutil.copytree(FIXTURE_REPO, repo)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "python p0 fixture")

    config = load_config(str(repo))
    config.repo_path = str(repo)
    config.db_path = str(work / "index.db")
    server = create_server(config)
    tools = server._tool_manager._tools

    rebuild = json.loads(tools["rebuild_index"].fn())
    assert rebuild["full_rebuild"] is True
    assert rebuild["nodes"] > 0
    assert rebuild["edges"] > 0
    yield tools


def _load_case(case_file: Path) -> dict:
    return json.loads(case_file.read_text(encoding="utf-8"))


def _query(tools: dict, case: dict) -> dict:
    return json.loads(
        tools["query_graph"].fn(
            qualified_name=case["qualified_name"],
            edge_kind=case["edge_kind"],
            direction=case["direction"],
            max_neighbors=case.get("max_per_dir", 20),
        )
    )


def _qnames(result: dict, direction: str) -> set[str]:
    return {node["qname"] for node in result[direction]}


@pytest.mark.parametrize("case_file", CASE_FILES, ids=lambda path: path.stem)
def test_query_case_uses_public_service(case_file: Path, query_tools: dict):
    case = _load_case(case_file)
    result = _query(query_tools, case)

    assert result["qname"] == case["qualified_name"]
    assert result.get("found", True) is not False
    assert _qnames(result, "in") == set(case["expected_in"])
    assert _qnames(result, "out") == set(case["expected_out"])


def test_query_contract_direction_limit_and_not_found(query_tools: dict):
    query_tool = query_tools["query_graph"].fn

    incoming = json.loads(
        query_tool(
            qualified_name="p0_fixture.modules.api::cross_target",
            edge_kind="call",
            direction="in",
            max_neighbors=20,
        )
    )
    assert incoming["out"] == []
    assert _qnames(incoming, "in") == {
        "p0_fixture.calls.cross_module::cross_consumer",
        "p0_fixture.calls.imports::import_consumer",
        "p0_fixture.modules.api::aggregate",
    }

    outgoing = json.loads(
        query_tool(
            qualified_name="p0_fixture.modules.api::aggregate",
            edge_kind="call",
            direction="out",
            max_neighbors=20,
        )
    )
    assert outgoing["in"] == []
    assert _qnames(outgoing, "out") == {"p0_fixture.modules.api::cross_target"}

    limited = json.loads(
        query_tool(
            qualified_name="p0_fixture.modules.api::cross_target",
            edge_kind="call",
            direction="in",
            max_neighbors=2,
        )
    )
    assert len(limited["in"]) == 2

    missing = json.loads(
        query_tool(
            qualified_name="p0_fixture.missing::symbol",
            edge_kind="call",
            direction="both",
        )
    )
    assert missing == {
        "qname": "p0_fixture.missing::symbol",
        "found": False,
        "in": [],
        "out": [],
    }


def test_query_contract_rejects_invalid_public_arguments(query_tools: dict):
    query_tool = query_tools["query_graph"].fn
    with pytest.raises(ValueError, match="edge_kind"):
        query_tool(
            qualified_name="p0_fixture.modules.api::cross_target",
            edge_kind="bogus",
        )
    with pytest.raises(ValueError, match="direction"):
        query_tool(
            qualified_name="p0_fixture.modules.api::cross_target",
            direction="sideways",
        )


def test_all_is_the_deduplicated_union_of_specific_resolved_kinds(query_tools: dict):
    query_tool = query_tools["query_graph"].fn
    qname = "p0_fixture.modules.api::aggregate"
    all_result = json.loads(
        query_tool(qualified_name=qname, edge_kind="all", direction="both")
    )
    specific = [
        json.loads(query_tool(qualified_name=qname, edge_kind=kind, direction="both"))
        for kind in ("call", "contains", "import", "extends")
    ]
    assert _qnames(all_result, "in") == set().union(
        *(_qnames(result, "in") for result in specific)
    )
    assert _qnames(all_result, "out") == set().union(
        *(_qnames(result, "out") for result in specific)
    )


def test_negative_calls_do_not_create_dynamic_resolved_neighbors(query_tools: dict):
    query_tool = query_tools["query_graph"].fn
    result = json.loads(
        query_tool(
            qualified_name="p0_fixture.negative.dynamic::negative_consumer",
            edge_kind="call",
            direction="out",
        )
    )
    assert _qnames(result, "out") == {
        "p0_fixture.negative.dynamic::normal_target",
        "p0_fixture.negative.dynamic::apply_callback",
    }
    assert "getattr" not in _qnames(result, "out")
    assert "importlib" not in _qnames(result, "out")


def test_python_method_constructor_and_super_edges_are_publicly_queryable(
    query_tools: dict,
):
    query_tool = query_tools["query_graph"].fn

    constructor = json.loads(
        query_tool(
            qualified_name="p0_fixture.classes.worker::Worker",
            edge_kind="call",
            direction="in",
        )
    )
    assert _qnames(constructor, "in") == {
        "p0_fixture.classes.worker::method_and_constructor",
        "p0_fixture.classes.worker::static_method_call",
    }

    super_call = json.loads(
        query_tool(
            qualified_name="p0_fixture.inheritance.base::Base.run",
            edge_kind="call",
            direction="in",
        )
    )
    assert _qnames(super_call, "in") == {
        "p0_fixture.inheritance.child::Child.run",
    }


def test_case_ids_are_unique_and_fixture_is_shared():
    cases = [_load_case(case_file) for case_file in CASE_FILES]
    case_ids = [case["case_id"] for case in cases]
    assert len(case_ids) == len(set(case_ids))
    assert set(case_ids) == REQUIRED_CASES
    assert {case["fixture"] for case in cases} == {"fixture_repo"}


def test_coverage_manifest_is_a_ci_gate_for_all_python_p0_cases():
    manifest = json.loads(COVERAGE_FILE.read_text(encoding="utf-8"))
    items = manifest["items"]
    loaded_ids = {_load_case(case_file)["case_id"] for case_file in CASE_FILES}
    applicable = {
        item["case_id"]
        for item in items
        if item["status"] != "not_applicable"
    }

    assert manifest["language"] == "python"
    assert applicable == REQUIRED_CASES
    assert all(item["status"] in {"covered", "missing", "partial", "not_applicable"}
               for item in items)
    assert all(item["status"] == "covered"
               for item in items if item["status"] != "not_applicable")
    assert [item for item in items if item["capability"] == "P0-G05"] == [
        {
            "capability": "P0-G05",
            "case_id": "py_implements_edges",
            "status": "not_applicable",
            "reason": "Python has no implements edge",
        }
    ]
    assert loaded_ids == applicable
    assert all(item.get("evidence") for item in items if item["status"] == "covered")


def test_p0_metrics_report_records_a_complete_public_e2e_suite():
    report = json.loads(METRICS_FILE.read_text(encoding="utf-8"))
    assert report["language"] == "python"
    assert report["suite"] == "query_p0"
    assert report["metrics"] == {
        "resolved_edge_recall": 1.0,
        "resolved_edge_precision": 1.0,
        "negative_edge_correctness": 1.0,
        "case_coverage": 1.0,
        "overall_correctness": 1.0,
    }
    assert report["counts"] == {
        "resolved_neighbor_assertions": 26,
        "resolved_neighbor_assertions_passed": 26,
        "negative_mechanisms": 3,
        "negative_mechanisms_without_phantom_neighbors": 3,
        "applicable_cases": 10,
        "cases_with_passing_e2e": 10,
        "overall_assertions": 29,
        "overall_assertions_passed": 29,
    }
