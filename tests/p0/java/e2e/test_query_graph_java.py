from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from code_review_ai.config import load_config
from code_review_ai.mcp_server import create_server
from p0_conformance import score_query_conformance


ROOT = Path(__file__).resolve().parents[4]
FIXTURE_REPO = ROOT / "tests" / "p0" / "java" / "fixture_repo"
CASE_DIR = ROOT / "tests" / "p0" / "java" / "cases" / "query"
CASE_FILES = tuple(sorted(CASE_DIR.glob("*.json")))
COVERAGE_FILE = ROOT / "tests" / "p0" / "java" / "p0-java-coverage.json"
METRICS_FILE = ROOT / "tests" / "p0" / "java" / "p0-java-metrics.json"

REQUIRED_CASES = {
    "java_call_same_class_and_control_flow",
    "java_call_static_and_constructor",
    "java_call_cross_package_and_static_import",
    "java_call_overload_and_scope",
    "java_call_recursion",
    "java_contains_edges",
    "java_import_edges",
    "java_extends_edges",
    "java_implements_edges",
    "java_all_edges",
    "java_query_contract",
    "java_nonresolved_call_edges",
    "JAVA-CALL-VAR-POS",
    "JAVA-CONTAINS-BOUNDARY",
    "JAVA-IMPORT-BOUNDARY",
    "JAVA-EXTENDS-BOUNDARY",
    "JAVA-IMPLEMENTS-BOUNDARY",
    "JAVA-CALL-SUPER-POS",
    "JAVA-CALL-MUTUAL-RECURSION-POS",
    "JAVA-CONTAINS-INTERFACE-METHOD-POS",
    "JAVA-CALL-INNER-CLASS-POS",
    "JAVA-CALL-LAMBDA-POS",
    "JAVA-CALL-ANONYMOUS-CLASS-POS",
    "JAVA-CONTAINS-ENUM-MEMBER-POS",
    "JAVA-CONTAINS-RECORD-COMPONENT-POS",
    "JAVA-CONTAINS-INITIALIZER-POS",
    "JAVA-CALL-ENUM-BODY-POS",
    "JAVA-CALL-INTERFACE-POS",
    "JAVA-CALL-RECORD-CTOR-POS",
    "JAVA-CALL-LOCAL-CLASS-POS",
    "JAVA-CALL-RETURN-CHAIN-POS",
    "JAVA-CALL-METHOD-REFERENCE-BOUNDARY",
    "JAVA-CALL-ABSTRACT-POS",
    "JAVA-CALL-GENERIC-POS",
    "JAVA-EXTENDS-SEALED-POS",
    "JAVA-IMPLEMENTS-SEALED-POS",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=java-p0@example.test",
            "-c",
            "user.name=java-p0",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def query_tools(tmp_path_factory: pytest.TempPathFactory):
    """Build one isolated public index and reuse it for every case."""
    work = tmp_path_factory.mktemp("java-p0")
    repo = work / "fixture_repo"
    shutil.copytree(FIXTURE_REPO, repo)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "java p0 fixture")

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
            qualified_name="com.acme.p0.calls::FlowCaller.bareTarget",
            edge_kind="call",
            direction="in",
            max_neighbors=20,
        )
    )
    assert incoming["out"] == []
    assert _qnames(incoming, "in") == {"com.acme.p0.calls::FlowCaller.run"}

    outgoing = json.loads(
        query_tool(
            qualified_name="com.acme.p0.calls::FlowCaller.run",
            edge_kind="call",
            direction="out",
            max_neighbors=20,
        )
    )
    assert outgoing["in"] == []
    assert len(outgoing["out"]) == 15

    limited = json.loads(
        query_tool(
            qualified_name="com.acme.p0.calls::FlowCaller.run",
            edge_kind="call",
            direction="out",
            max_neighbors=2,
        )
    )
    assert _qnames(limited, "out") == {
        "com.acme.p0.calls::FlowCaller.bareTarget",
        "com.acme.p0.calls::FlowCaller.catchTarget",
    }

    missing = json.loads(
        query_tool(
            qualified_name="com.acme.p0.missing::symbol",
            edge_kind="call",
            direction="both",
        )
    )
    assert missing == {
        "qname": "com.acme.p0.missing::symbol",
        "found": False,
        "in": [],
        "out": [],
    }


def test_query_contract_rejects_invalid_public_arguments(query_tools: dict):
    query_tool = query_tools["query_graph"].fn
    with pytest.raises(ValueError, match="edge_kind"):
        query_tool(
            qualified_name="com.acme.p0.calls::FlowCaller.run",
            edge_kind="bogus",
        )
    with pytest.raises(ValueError, match="direction"):
        query_tool(
            qualified_name="com.acme.p0.calls::FlowCaller.run",
            direction="sideways",
        )


def test_all_is_the_deduplicated_union_of_specific_resolved_kinds(query_tools: dict):
    query_tool = query_tools["query_graph"].fn
    qname = "com.acme.p0.inheritance::DerivedService"
    all_result = json.loads(
        query_tool(qualified_name=qname, edge_kind="all", direction="both")
    )
    specific = [
        json.loads(
            query_tool(qualified_name=qname, edge_kind=kind, direction="both")
        )
        for kind in ("call", "contains", "import", "extends", "implements")
    ]
    assert _qnames(all_result, "in") == set().union(
        *(_qnames(result, "in") for result in specific)
    )
    assert _qnames(all_result, "out") == set().union(
        *(_qnames(result, "out") for result in specific)
    )


def test_negative_calls_do_not_create_reflection_proxy_or_argument_targets(
    query_tools: dict,
):
    case = _load_case(CASE_DIR / "java_nonresolved_call_edges.json")
    assert set(case["negative_kinds"]) == {
        "reflection",
        "dynamic_proxy",
        "functional_argument",
    }
    result = _query(query_tools, case)
    assert _qnames(result, "out") == set(case["expected_out"])
    assert "java.lang.reflect.Method.invoke" not in _qnames(result, "out")
    assert "java.lang.reflect.Proxy.newProxyInstance" not in _qnames(result, "out")


def test_case_ids_are_unique_and_fixture_is_shared():
    cases = [_load_case(case_file) for case_file in CASE_FILES]
    case_ids = [case["case_id"] for case in cases]
    assert len(case_ids) == len(set(case_ids))
    assert set(case_ids) == REQUIRED_CASES
    assert {case["fixture"] for case in cases} == {"fixture_repo"}
    assert all(case["language"] == "java" for case in cases)


def test_coverage_manifest_is_a_ci_gate_for_all_java_p0_cases():
    manifest = json.loads(COVERAGE_FILE.read_text(encoding="utf-8"))
    items = manifest["items"]
    loaded_ids = {_load_case(case_file)["case_id"] for case_file in CASE_FILES}
    assert manifest["language"] == "java"
    assert {item["case_id"] for item in items} == REQUIRED_CASES
    assert {item["case_id"] for item in items} == loaded_ids
    assert all(item["status"] in {"covered", "missing", "partial"}
               for item in items)
    assert all(item["status"] == "covered" for item in items)
    assert all(item.get("evidence") for item in items)
    assert all(item["case_id"] in loaded_ids for item in items)
    assert {
        evidence for item in items for evidence in item["evidence"]
    } <= loaded_ids


def test_p0_metrics_report_is_reproduced_from_public_queries(
    query_tools: dict,
):
    report = json.loads(METRICS_FILE.read_text(encoding="utf-8"))
    cases = [_load_case(case_file) for case_file in CASE_FILES]
    actual = score_query_conformance(
        query_tools["query_graph"].fn, cases, language="java")
    assert report == actual
