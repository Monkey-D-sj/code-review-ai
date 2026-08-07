"""Reproducible evaluation against historical change manifests."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from code_review_ai import qname
from code_review_ai.config import Config
from code_review_ai.impact import get_impact
from code_review_ai.indexer import RebuildStats, rebuild
from code_review_ai.parser import parse_file


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    changed_symbols: list[str]
    changed_ranges: dict[str, list[tuple[int, int]]]
    gold_files: list[str]
    repo: str | None = None
    base_commit: str | None = None


def load_cases(path: str) -> list[BenchmarkCase]:
    """Load and validate a JSON array of historical change cases."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("benchmark manifest must be a non-empty JSON array")
    return [_parse_case(record, position) for position, record in enumerate(raw)]


def _parse_case(record: object, position: int) -> BenchmarkCase:
    if not isinstance(record, dict):
        raise ValueError(f"case {position} must be an object")
    case_id = record.get("id")
    symbols = record.get("changed_symbols", [])
    changed_ranges = _parse_ranges(record.get("changed_ranges", {}), case_id)
    gold_files = record.get("gold_files")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError(f"case {position} requires a non-empty id")
    if not _optional_string_list(symbols) or not _string_list(gold_files):
        raise ValueError(f"case {case_id} has invalid symbols or gold_files")
    if not symbols and not changed_ranges:
        raise ValueError(f"case {case_id} requires changed_symbols or changed_ranges")
    return BenchmarkCase(case_id, symbols, changed_ranges,
                         [_normalize(path) for path in gold_files],
                         _optional_string(record.get("repo")),
                         _optional_string(record.get("base_commit")))


def _string_list(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and bool(item) for item in value
    )


def _optional_string_list(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and bool(item) for item in value
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_ranges(value: object, case_id: object) -> dict[str, list[tuple[int, int]]]:
    if not isinstance(value, dict):
        raise ValueError(f"case {case_id} changed_ranges must be an object")
    parsed: dict[str, list[tuple[int, int]]] = {}
    for path, ranges in value.items():
        if not isinstance(path, str) or not isinstance(ranges, list):
            raise ValueError(f"case {case_id} has invalid changed_ranges")
        parsed[_normalize(path)] = [_parse_range(item, case_id) for item in ranges]
    return parsed


def _parse_range(value: object, case_id: object) -> tuple[int, int]:
    valid = (isinstance(value, list) and len(value) == 2
             and all(isinstance(line, int) and line >= 1 for line in value))
    if not valid or value[0] > value[1]:
        raise ValueError(f"case {case_id} has invalid line range")
    return value[0], value[1]


def run_benchmark(config: Config, conn: sqlite3.Connection,
                  cases: list[BenchmarkCase], top_k: int = 10) -> dict:
    """Rebuild the index and evaluate impact-file recall for every case."""
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    if not cases:
        raise ValueError("at least one benchmark case is required")
    stats = rebuild(config, conn)
    results = [_evaluate_case(config, conn, case, top_k) for case in cases]
    return {
        "schema_version": 2,
        "repository": str(Path(config.repo_path).resolve()),
        "top_k": top_k,
        "index": _index_metrics(config, conn, stats),
        "aggregate": _aggregate(results),
        "cases": results,
    }


def _classify_golds(config: Config, case: BenchmarkCase) -> tuple[list[str], list[str]]:
    """Split gold test files into (direct, co-change).

    A gold is 'direct' when its source mentions (by name) a class defined in a
    changed production file — a graph-independent proxy for 'this test
    exercises the changed code'. Java same-package references need no import,
    so a plain import check misses them; word-boundary text matching covers
    imports, same-package refs, annotations, and static calls alike. Others are
    commit-level co-change (changed in the same commit but with no static
    reference to the changed classes)."""
    changed_classes: set[str] = set()
    for prod_file in case.changed_ranges:
        try:
            prod_pf = parse_file(os.path.join(config.repo_path, prod_file),
                                 config.repo_path)
        except (OSError, ValueError):
            continue
        for node in prod_pf.nodes:
            if node.kind == "class":
                changed_classes.add(qname.short(node.qualified_name))
    patterns = [re.compile(rf"\b{re.escape(name)}\b") for name in changed_classes]
    direct: list[str] = []
    cochange: list[str] = []
    for gold in case.gold_files:
        try:
            text = Path(os.path.join(config.repo_path, gold)).read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            cochange.append(gold)
            continue
        if any(pattern.search(text) for pattern in patterns):
            direct.append(gold)
        else:
            cochange.append(gold)
    return direct, cochange


def _evaluate_case(config: Config, conn: sqlite3.Connection,
                   case: BenchmarkCase, top_k: int) -> dict:
    symbols = _case_symbols(config, conn, case)
    started = time.perf_counter()
    impacts = _all_impacts(conn, symbols)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    all_candidates = _candidate_files(config, conn, impacts)
    candidates = all_candidates[:top_k]
    test_score = _score_candidates(candidates, case.gold_files)
    all_test_score = _score_candidates(all_candidates, case.gold_files)
    direct_golds, cochange_golds = _classify_golds(config, case)
    direct_score = _score_candidates(all_candidates, direct_golds)
    found_count = sum(1 for impact in impacts if impact["found"])
    return {
        "id": case.case_id,
        "repo": case.repo,
        "base_commit": case.base_commit,
        "changed_symbols": symbols,
        "gold_files": case.gold_files,
        "candidate_files": candidates,
        "hits": test_score["hits"],
        "patch_file_recall_at_k": test_score["recall"],
        "patch_file_precision_at_k": test_score["precision"],
        "all_candidate_files_count": len(all_candidates),
        "all_hits": all_test_score["hits"],
        "patch_file_recall_all": all_test_score["recall"],
        "patch_file_precision_all": all_test_score["precision"],
        "direct_gold_files": direct_golds,
        "cochange_gold_files": cochange_golds,
        "direct_recall_all": direct_score["recall"],
        "symbol_found_rate": round(found_count / len(impacts), 4)
        if impacts else 0.0,
        "query_ms": elapsed_ms,
        "production_file_folds": _production_folds(
            config, conn, case.changed_ranges, top_k
        ),
    }


def _case_symbols(config: Config, conn: sqlite3.Connection,
                  case: BenchmarkCase) -> list[str]:
    symbols = list(case.changed_symbols)
    symbols.extend(_symbols_for_ranges(config, conn, case.changed_ranges))
    return list(dict.fromkeys(symbols))


def _symbols_for_ranges(config: Config, conn: sqlite3.Connection,
                        changed_ranges: dict[str, list[tuple[int, int]]]) -> list[str]:
    symbols: list[str] = []
    for file_path, ranges in changed_ranges.items():
        rows = conn.execute(
            "SELECT qualified_name,start_line,end_line FROM nodes "
            "WHERE REPLACE(file_path, CHAR(92), '/') LIKE ? "
            "AND kind IN ('function','method','class') ORDER BY start_line",
            (f"%/{file_path}",),
        ).fetchall()
        for row in rows:
            if _overlaps_any(row["start_line"], row["end_line"], ranges):
                symbols.append(row["qualified_name"])
    return list(dict.fromkeys(symbols))


def _production_folds(config: Config, conn: sqlite3.Connection,
                      changed_ranges: dict[str, list[tuple[int, int]]],
                      top_k: int) -> list[dict]:
    if len(changed_ranges) < 2:
        return []
    folds: list[dict] = []
    all_files = list(changed_ranges)
    for seed_file, ranges in changed_ranges.items():
        symbols = _symbols_for_ranges(config, conn, {seed_file: ranges})
        started = time.perf_counter()
        impacts = _all_impacts(conn, symbols)
        query_ms = round((time.perf_counter() - started) * 1000, 3)
        all_candidates = [path for path in _candidate_files(config, conn, impacts)
                          if path != seed_file]
        candidates = all_candidates[:top_k]
        gold_files = [path for path in all_files if path != seed_file]
        score = _score_candidates(candidates, gold_files)
        all_score = _score_candidates(all_candidates, gold_files)
        folds.append({"seed_file": seed_file, "changed_symbols": symbols,
                      "gold_files": gold_files, "candidate_files": candidates,
                      "hits": score["hits"], "recall_at_k": score["recall"],
                      "precision_at_k": score["precision"],
                      "all_candidate_files_count": len(all_candidates),
                      "all_hits": all_score["hits"],
                      "recall_all": all_score["recall"],
                      "precision_all": all_score["precision"],
                      "query_ms": query_ms})
    return folds


def _score_candidates(candidates: list[str], gold_files: list[str]) -> dict:
    gold = set(gold_files)
    hits = [path for path in candidates if path in gold]
    return {
        "hits": hits,
        "recall": round(len(set(hits)) / len(gold), 4) if gold else 0.0,
        "precision": round(len(set(hits)) / len(candidates), 4)
        if candidates else 0.0,
    }


def _all_impacts(conn: sqlite3.Connection, symbols: list[str]) -> list[dict]:
    """Impact with every node retained (tests='include').

    The default 'exclude' filter drops is_test=1 nodes from upstream/downstream,
    which would zero out test-file recall once Python test files are tagged as
    tests (test_globs). The benchmark measures whether gold test files are
    retrieved, so tests must be included."""
    node_count = conn.execute("SELECT COUNT(*) AS count FROM nodes").fetchone()["count"]
    return get_impact(conn, symbols, max_nodes_per_direction=max(node_count, 1),
                      tests="include")


def _overlaps_any(start: int, end: int,
                  ranges: list[tuple[int, int]]) -> bool:
    return any(start <= range_end and end >= range_start
               for range_start, range_end in ranges)


def _candidate_files(config: Config, conn: sqlite3.Connection,
                     impacts: list[dict]) -> list[str]:
    candidates: list[str] = []
    for impact in impacts:
        changed_file = _symbol_file(config, conn, impact["symbol"])
        if changed_file:
            candidates.append(changed_file)
        for direction in ("upstream", "downstream"):
            candidates.extend(_relative(config, node["file"])
                              for node in impact[direction] if node["file"])
    return list(dict.fromkeys(candidates))


def _symbol_file(config: Config, conn: sqlite3.Connection, symbol: str) -> str:
    row = conn.execute(
        "SELECT file_path FROM nodes WHERE qualified_name=?", (symbol,),
    ).fetchone()
    return _relative(config, row["file_path"]) if row else ""


def _relative(config: Config, file_path: str) -> str:
    path = Path(file_path)
    try:
        return _normalize(str(path.resolve().relative_to(Path(config.repo_path).resolve())))
    except ValueError:
        return _normalize(file_path)


def _normalize(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _index_metrics(config: Config, conn: sqlite3.Connection,
                   stats: RebuildStats) -> dict:
    rows = conn.execute(
        "SELECT resolution, COUNT(*) AS count FROM edges "
        "WHERE kind='call' GROUP BY resolution"
    ).fetchall()
    resolutions = {row["resolution"]: row["count"] for row in rows}
    total_calls = sum(resolutions.values())
    return {
        "source_files": _source_file_count(conn),
        "nodes": stats.node_count,
        "edges": stats.edge_count,
        "flows": stats.flow_count,
        "communities": stats.community_count,
        "timings_ms": stats.stage_timings,
        "database_bytes": _database_size(config.db_path),
        "call_resolutions": resolutions,
        "resolved_call_rate": round(resolutions.get("resolved", 0) / total_calls, 4)
        if total_calls else 0.0,
    }


def _source_file_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(DISTINCT file_path) AS count FROM nodes").fetchone()
    return row["count"]


def _database_size(db_path: str) -> int:
    return os.path.getsize(db_path) if os.path.exists(db_path) else 0


def _aggregate(results: list[dict]) -> dict:
    case_count = len(results)
    production_folds = [fold for result in results
                        for fold in result["production_file_folds"]]
    return {
        "cases": case_count,
        "macro_patch_file_recall_at_k": round(sum(
            result["patch_file_recall_at_k"] for result in results
        ) / case_count, 4),
        "macro_patch_file_precision_at_k": round(sum(
            result["patch_file_precision_at_k"] for result in results
        ) / case_count, 4),
        "macro_patch_file_recall_all": round(sum(
            result["patch_file_recall_all"] for result in results
        ) / case_count, 4),
        "macro_patch_file_precision_all": round(sum(
            result["patch_file_precision_all"] for result in results
        ) / case_count, 4),
        "macro_direct_test_file_recall_all": _direct_recall_mean(results),
        "cochange_gold_count": sum(
            len(result["cochange_gold_files"]) for result in results),
        "mean_all_candidate_files": round(sum(
            result["all_candidate_files_count"] for result in results
        ) / case_count, 2),
        "symbol_found_rate": round(
            sum(result["symbol_found_rate"] for result in results) / case_count, 4),
        "mean_query_ms": round(
            sum(result["query_ms"] for result in results) / case_count, 3),
        "production_file_eligible_cases": sum(
            bool(result["production_file_folds"]) for result in results),
        "production_file_folds": len(production_folds),
        "macro_related_production_file_recall_at_k": _fold_mean(
            production_folds, "recall_at_k"),
        "macro_related_production_file_precision_at_k": _fold_mean(
            production_folds, "precision_at_k"),
        "macro_related_production_file_recall_all": _fold_mean(
            production_folds, "recall_all"),
        "macro_related_production_file_precision_all": _fold_mean(
            production_folds, "precision_all"),
        "mean_production_all_candidate_files": _fold_mean(
            production_folds, "all_candidate_files_count"),
    }


def _direct_recall_mean(results: list[dict]) -> float | None:
    """Macro direct-test recall over cases that have at least one direct gold."""
    with_direct = [result for result in results if result["direct_gold_files"]]
    if not with_direct:
        return None
    return round(sum(result["direct_recall_all"]
                     for result in with_direct) / len(with_direct), 4)


def _fold_mean(folds: list[dict], key: str) -> float | None:
    if not folds:
        return None
    return round(sum(fold[key] for fold in folds) / len(folds), 4)
