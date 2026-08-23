"""Shared scoring for the synthetic P0 edge-conformance suites."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path


def _qnames(result: dict, direction: str) -> set[str]:
    return {node["qname"] for node in result[direction]}


def _negative_count(case: dict) -> int:
    kinds = case.get("negative_kinds")
    if kinds is not None:
        return len(set(kinds))
    return 1 if case.get("negative_kind") else 0


def score_query_conformance(
    query_tool: Callable[..., str],
    cases: Iterable[dict],
    *,
    language: str,
) -> dict:
    """Execute public query cases and derive TP/FP/FN metrics.

    Directional neighbors are distinct graph facts. Dynamic/reflection cases
    have no unique gold target, so they contribute only to negative-edge
    correctness and never lower resolved-edge recall.
    """
    case_list = list(cases)
    tp = fp = fn = returned = gold = 0
    exact_cases = 0
    negative_total = 0
    negative_passed = 0

    for case in case_list:
        result = json.loads(query_tool(
            qualified_name=case["qualified_name"],
            edge_kind=case["edge_kind"],
            direction=case["direction"],
            max_neighbors=case.get("max_per_dir", 20),
        ))
        case_fp = 0
        exact = True
        for direction in ("in", "out"):
            expected = set(case[f"expected_{direction}"])
            actual = _qnames(result, direction)
            gold += len(expected)
            returned += len(actual)
            tp += len(actual & expected)
            missing = expected - actual
            extra = actual - expected
            fn += len(missing)
            fp += len(extra)
            case_fp += len(extra)
            exact = exact and not missing and not extra
        if exact:
            exact_cases += 1
        negatives = _negative_count(case)
        negative_total += negatives
        if negatives and case_fp == 0:
            negative_passed += negatives

    def ratio(numerator: int, denominator: int) -> float:
        return 1.0 if denominator == 0 else numerator / denominator

    return {
        "language": language,
        "suite": "query_p0",
        "metrics": {
            "resolved_edge_recall": ratio(tp, tp + fn),
            "resolved_edge_precision": ratio(tp, tp + fp),
            "negative_edge_correctness": ratio(
                negative_passed, negative_total),
            "exact_case_pass_rate": ratio(exact_cases, len(case_list)),
        },
        "counts": {
            "gold_resolved_neighbors": gold,
            "returned_resolved_neighbors": returned,
            "true_positives": tp,
            "false_positives": fp,
            "false_negatives": fn,
            "negative_mechanisms": negative_total,
            "negative_mechanisms_without_phantom_neighbors": negative_passed,
            "registered_cases": len(case_list),
            "exact_cases": exact_cases,
        },
        "denominators": {
            "resolved_edge_recall": "TP / (TP + FN) over directional gold resolved neighbors",
            "resolved_edge_precision": "TP / (TP + FP) over directional returned resolved neighbors",
            "negative_edge_correctness": "dynamic/reflection mechanisms whose case produced no undeclared resolved neighbor",
            "exact_case_pass_rate": "cases whose complete in/out neighbor sets equal gold",
        },
    }


VALID_LANGUAGES = {"python", "typescript", "java"}
VALID_EDGE_KINDS = {"call", "contains", "import", "extends", "implements"}
VALID_CLASSIFICATIONS = {"static", "module", "type", "ambiguous", "runtime"}
VALID_STATUSES = {"covered", "partial", "missing", "dynamic", "not_applicable"}


def load_p0_cases(root: Path) -> list[dict]:
    """Load and normalize every public P0 query case.

    TypeScript uses grouped case files while Python and Java mostly use one
    case per file.  The catalog validator deliberately sees one normalized
    record per case so the reverse ``syntax_ids`` relation is unambiguous.
    """
    cases: list[dict] = []
    for language in ("python", "typescript", "java"):
        case_dir = root / "tests" / "p0" / language / "cases" / "query"
        for case_file in sorted(case_dir.glob("*.json")):
            payload = json.loads(case_file.read_text(encoding="utf-8"))
            raw_cases = payload.get("cases", [payload])
            for raw in raw_cases:
                case = {**payload, **raw}
                case.pop("cases", None)
                case["language"] = language
                case["case_file"] = str(case_file.relative_to(root))
                cases.append(case)
    return cases


def validate_syntax_catalog(catalog: dict, cases: Iterable[dict]) -> list[str]:
    """Return deterministic catalog/case contract violations."""
    errors: list[str] = []
    items = catalog.get("items")
    if not isinstance(items, list):
        return ["catalog.items must be a list"]
    ids = [item.get("id") for item in items]
    if len(ids) != len(set(ids)):
        errors.append("catalog item IDs must be globally unique")
    by_id = {item.get("id"): item for item in items}
    case_list = list(cases)
    case_by_id = {case.get("case_id"): case for case in case_list}
    if len(case_by_id) != len(case_list):
        errors.append("case IDs must be globally unique")

    for item in items:
        item_id = item.get("id", "<missing>")
        language = item.get("language")
        edge_kind = item.get("edge_kind")
        status = item.get("status")
        classification = item.get("classification")
        if language not in VALID_LANGUAGES:
            errors.append(f"{item_id}: invalid language {language!r}")
        if edge_kind not in VALID_EDGE_KINDS:
            errors.append(f"{item_id}: invalid edge_kind {edge_kind!r}")
        if status not in VALID_STATUSES:
            errors.append(f"{item_id}: invalid status {status!r}")
        if classification not in VALID_CLASSIFICATIONS:
            errors.append(f"{item_id}: invalid classification {classification!r}")
        if status == "partial" and not item.get("limitations"):
            errors.append(f"{item_id}: partial item must document limitations")
        if status == "dynamic" and not item.get("limitations"):
            errors.append(f"{item_id}: dynamic item must document limitations")
        if language == "python" and edge_kind == "implements" and status != "not_applicable":
            errors.append(f"{item_id}: Python implements must be not_applicable")
        if status == "not_applicable" and item.get("case_ids"):
            errors.append(f"{item_id}: not_applicable item cannot require cases")
        for case_id in item.get("case_ids", []):
            case = case_by_id.get(case_id)
            if case is None:
                errors.append(f"{item_id}: unknown case_id {case_id!r}")
            elif case.get("language") != language:
                errors.append(f"{item_id}: case {case_id!r} has wrong language")

    for case in case_list:
        case_id = case.get("case_id", "<missing>")
        syntax_ids = case.get("syntax_ids")
        if not isinstance(syntax_ids, list) or not syntax_ids:
            errors.append(f"{case_id}: syntax_ids must be a non-empty list")
            continue
        for syntax_id in syntax_ids:
            item = by_id.get(syntax_id)
            if item is None:
                errors.append(f"{case_id}: unknown syntax_id {syntax_id!r}")
            elif case.get("language") != item.get("language"):
                errors.append(f"{case_id}: syntax_id {syntax_id!r} has wrong language")
            elif case_id not in item.get("case_ids", []):
                errors.append(f"{case_id}: syntax_id {syntax_id!r} does not reverse-link case")

    for item in items:
        if item.get("status") != "covered":
            continue
        linked = [case_by_id[cid] for cid in item.get("case_ids", []) if cid in case_by_id]
        has_positive = any(not (case.get("negative_kind") or case.get("negative_kinds"))
                           for case in linked)
        has_boundary = any(case.get("negative_kind") or case.get("negative_kinds")
                           for case in linked)
        if not (has_positive and has_boundary):
            errors.append(f"{item.get('id')}: covered requires positive and boundary evidence")
    return sorted(set(errors))


def catalog_metrics(catalog: dict, cases: Iterable[dict]) -> dict:
    """Compute denominator metrics solely from catalog and registered cases."""
    case_list = list(cases)
    by_language_edge: dict[tuple[str, str], dict[str, int]] = {}
    for item in catalog["items"]:
        key = (item["language"], item["edge_kind"])
        counts = by_language_edge.setdefault(key, {status: 0 for status in VALID_STATUSES})
        counts[item["status"]] += 1

    rows = []
    for (language, edge_kind), counts in sorted(by_language_edge.items()):
        denominator = counts["covered"] + counts["partial"] + counts["missing"]
        rows.append({
            "language": language,
            "edge_kind": edge_kind,
            **counts,
            "static_coverage": (counts["covered"] / denominator) if denominator else 1.0,
        })
    dynamic_items = [item for item in catalog["items"] if item["status"] == "dynamic"]
    case_ids = {case.get("case_id") for case in case_list
                if case.get("negative_kind") or case.get("negative_kinds")}
    dynamic_with_boundary = sum(bool(set(item.get("case_ids", [])) & case_ids)
                                for item in dynamic_items)
    return {
        "schema_version": 1,
        "denominator": "syntax-catalog.json",
        "rows": rows,
        "totals": {status: sum(item["status"] == status for item in catalog["items"])
                   for status in sorted(VALID_STATUSES)},
        "registered_cases": len(case_list),
        "dynamic_honesty": (dynamic_with_boundary / len(dynamic_items)
                             if dynamic_items else 1.0),
    }


def render_syntax_report(catalog: dict, cases: Iterable[dict]) -> str:
    """Render the checked-in conformance report from catalog-derived metrics."""
    metrics = catalog_metrics(catalog, cases)
    lines = [
        "# P0 Edge Syntax Conformance Report",
        "",
        "> Generated from `tests/p0/syntax-catalog.json` and the public P0 case files.",
        "> Do not edit the metric rows by hand; regenerate them through `render_syntax_report`.",
        "",
        "| Language | Edge | Covered | Partial | Missing | Dynamic | N/A | Static Coverage |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in metrics["rows"]:
        lines.append(
            "| {language} | {edge_kind} | {covered} | {partial} | {missing} | "
            "{dynamic} | {not_applicable} | {static_coverage:.2%} |".format(**row)
        )
    lines.extend([
        "",
        "## Totals",
        "",
        f"- Catalog items: {sum(metrics['totals'].values())}",
        f"- Registered public cases: {metrics['registered_cases']}",
        f"- Dynamic Honesty evidence: {metrics['dynamic_honesty']:.2%}",
        "- `not_applicable` is excluded from Static Coverage.",
        "- `partial` is not counted as `covered`.",
        "",
        "A syntax item can move to `covered` only after the catalog validator sees "
        "both a positive case and a boundary case, with complete public query "
        "neighbor sets. Dynamic and candidate targets remain outside resolved query results.",
        "",
    ])
    return "\n".join(lines)
