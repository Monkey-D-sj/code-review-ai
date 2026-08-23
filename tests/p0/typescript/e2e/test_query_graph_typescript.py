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
FIXTURE_REPO = ROOT / "tests" / "p0" / "typescript" / "fixture_repo"
CASE_DIR = ROOT / "tests" / "p0" / "typescript" / "cases" / "query"
CASE_FILES = tuple(sorted(CASE_DIR.glob("*.json")))
COVERAGE_FILE = ROOT / "tests" / "p0" / "typescript" / "p0-typescript-coverage.json"
METRICS_FILE = ROOT / "tests" / "p0" / "typescript" / "p0-typescript-metrics.json"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=ts-p0@example.test",
         "-c", "user.name=typescript-p0", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def query_tools(tmp_path_factory: pytest.TempPathFactory):
    work = tmp_path_factory.mktemp("typescript-p0")
    repo = work / "fixture_repo"
    shutil.copytree(FIXTURE_REPO, repo)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "typescript p0 fixture")

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


def _load_cases(case_file: Path) -> list[dict]:
    group = json.loads(case_file.read_text(encoding="utf-8"))
    cases = []
    for case in group["cases"]:
        cases.append({
            **group,
            **case,
            "edge_kind": case.get("edge_kind", group["edge_kind"]),
        })
    return cases


def _query(tools: dict, case: dict) -> dict:
    return json.loads(tools["query_graph"].fn(
        qualified_name=case["qualified_name"],
        edge_kind=case["edge_kind"],
        direction=case["direction"],
        max_neighbors=case.get("max_per_dir", 20),
    ))


ALL_CASES = tuple(
    case
    for case_file in CASE_FILES
    for case in _load_cases(case_file)
)


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda case: case["case_id"])
def test_query_case_uses_public_service(case: dict, query_tools: dict):
    result = _query(query_tools, case)

    assert result["qname"] == case["qualified_name"]
    assert result.get("found", True) is not False
    assert {node["qname"] for node in result["in"]} == set(case["expected_in"])
    assert {node["qname"] for node in result["out"]} == set(case["expected_out"])


def test_query_contract_direction_limit_and_not_found(query_tools: dict):
    query_tool = query_tools["query_graph"].fn

    incoming = json.loads(query_tool(
        qualified_name="api::fetchUser",
        edge_kind="call",
        direction="in",
        max_neighbors=20,
    ))
    assert incoming["out"] == []
    assert "calls::topLevelCall" in {node["qname"] for node in incoming["in"]}

    outgoing = json.loads(query_tool(
        qualified_name="calls::topLevelCall",
        edge_kind="call",
        direction="out",
        max_neighbors=20,
    ))
    assert outgoing["in"] == []
    assert [node["qname"] for node in outgoing["out"]] == ["api::fetchUser"]

    limited = json.loads(query_tool(
        qualified_name="api::fetchUser",
        edge_kind="call",
        direction="in",
        max_neighbors=2,
    ))
    assert {node["qname"] for node in limited["in"]} == {
        "calls::arrowCall",
        "calls::branchCall",
    }

    missing = json.loads(query_tool(
        qualified_name="missing::symbol",
        edge_kind="call",
        direction="both",
    ))
    assert missing == {
        "qname": "missing::symbol",
        "found": False,
        "in": [],
        "out": [],
    }


def test_query_contract_rejects_invalid_public_arguments(query_tools: dict):
    query_tool = query_tools["query_graph"].fn
    with pytest.raises(ValueError, match="edge_kind"):
        query_tool(qualified_name="api::fetchUser", edge_kind="bogus")
    with pytest.raises(ValueError, match="direction"):
        query_tool(qualified_name="api::fetchUser", direction="sideways")


def test_all_is_the_deduplicated_union_of_specific_resolved_kinds(query_tools: dict):
    query_tool = query_tools["query_graph"].fn
    qname = "user-store::UserStore"
    all_result = json.loads(
        query_tool(qualified_name=qname, edge_kind="all", direction="both")
    )
    specific = [
        json.loads(query_tool(qualified_name=qname, edge_kind=kind, direction="both"))
        for kind in ("call", "contains", "import", "extends", "implements")
    ]
    assert {node["qname"] for node in all_result["in"]} == set().union(
        *({node["qname"] for node in result["in"]} for result in specific)
    )
    assert {node["qname"] for node in all_result["out"]} == set().union(
        *({node["qname"] for node in result["out"]} for result in specific)
    )


def test_case_ids_are_unique_and_fixture_is_shared():
    case_ids = [case["case_id"] for case in ALL_CASES]
    assert len(case_ids) == len(set(case_ids))
    assert {case["fixture"] for case in ALL_CASES} == {"fixture_repo"}


def test_coverage_manifest_has_evidence_for_every_registered_query_case():
    manifest = json.loads(COVERAGE_FILE.read_text(encoding="utf-8"))
    items = manifest["items"]
    required = {
        "ts_call_edges",
        "ts_contains_edges",
        "ts_import_edges",
        "ts_extends_edges",
        "ts_implements_edges",
        "ts_all_edges",
        "ts_query_contract",
        "ts_nonresolved_call_edges",
    }
    assert {item["case_id"] for item in items} == required
    assert all(item["status"] in {"covered", "missing", "partial"}
               for item in items)
    assert all(item["status"] == "covered" for item in items)

    loaded_ids = {case["case_id"] for case in ALL_CASES}
    evidence_ids = {
        evidence
        for item in items
        for evidence in item.get("evidence", [])
    }
    assert evidence_ids <= loaded_ids


def test_p0_metrics_report_is_reproduced_from_public_queries(
    query_tools: dict,
):
    report = json.loads(METRICS_FILE.read_text(encoding="utf-8"))
    actual = score_query_conformance(
        query_tools["query_graph"].fn, ALL_CASES, language="typescript")
    assert report == actual
