"""Deterministic, local-only context planning for code review.

The planner deliberately has no prompt or gold-label input.  It classifies a
change from diff/AST/graph metadata, then builds one bounded evidence package
which can be inspected offline or supplied to a later single-shot reviewer.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from pathlib import Path

from code_review_ai.changes import build_change_summary, _resolve_diff_base
from code_review_ai.config import Config


DEFAULT_MAX_CHARS = 8_000
DEFAULT_MAX_NEIGHBORS = 6
DEFAULT_MAX_TESTS = 3
MIN_MAX_CHARS = 1_000

_DECLARATION_CHANGE = re.compile(
    r"^(?:[+\-])\s*(?:"
    r"(?:public|protected|private|static|final|abstract|async|export|default|"
    r"synchronized|native|override|virtual|sealed|non-sealed)\s+)*"
    r"(?:class|interface|record|enum|def|function)\b|"
    r"^(?:[+\-])\s*(?:@|\[Override\])"
)
_INHERITANCE_CHANGE = re.compile(r"^(?:[+\-]).*\b(?:extends|implements)\b")
_CAMEL_TOKEN = re.compile(r"[A-Z][a-z0-9]+|[A-Z]+(?=[A-Z]|$)|[a-z][a-z0-9]+")


def plan_context(
    config: Config,
    conn: sqlite3.Connection,
    *,
    diff: str | None = None,
    files: list[str] | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_neighbors: int = DEFAULT_MAX_NEIGHBORS,
    max_tests: int = DEFAULT_MAX_TESTS,
) -> dict:
    """Return a deterministic local/graph route and a bounded evidence pack.

    Routing uses only the repository diff, parsed change summary and the local
    graph index.  It never consumes the review task, complexity tags, gold
    labels or an LLM response.
    """
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) \
            or max_chars < MIN_MAX_CHARS:
        raise ValueError(f"max_chars must be an integer >= {MIN_MAX_CHARS}")
    if not isinstance(max_neighbors, int) or max_neighbors < 0:
        raise ValueError("max_neighbors must be a non-negative integer")
    if not isinstance(max_tests, int) or max_tests < 0:
        raise ValueError("max_tests must be a non-negative integer")

    raw_diff = diff if diff is not None else _git_diff(config, files)
    summary = build_change_summary(config, conn, files=files)
    records = _change_records(summary)
    route, reasons, graph_stats = _choose_route(conn, records, summary, raw_diff)

    evidence: list[dict] = []
    if raw_diff:
        evidence.append({
            "kind": "diff", "file": None, "priority": 100,
            "source": raw_diff,
        })
    evidence.extend(_changed_source(config, records))
    evidence.extend(_uncovered_evidence(summary))

    neighbors: list[dict] = []
    if route == "graph" and max_neighbors:
        neighbors = _graph_neighbors(config, conn, records, max_neighbors)
        evidence.extend(neighbors)

    tests = _target_tests(config, conn, records, max_tests)
    existing_test_files = {item.get("file") for item in evidence
                           if item.get("kind") == "test"}
    evidence.extend(item for item in tests
                    if item.get("file") not in existing_test_files)
    evidence = _dedupe_overlapping_evidence(evidence)

    payload = {
        "schema_version": 1,
        "planner": "deterministic-local-v1",
        "route": route,
        "reasons": reasons,
        "change": {
            **summary.get("summary", {}),
            "symbols": [record.get("qname") for record in records
                        if record.get("qname")],
            "files": sorted(_record_files(records, summary)),
        },
        "graph_stats": graph_stats,
        "evidence": evidence,
        "metrics": {
            "max_chars": max_chars,
            "serialized_chars": 0,
            "truncated": False,
            "evidence_entries": len(evidence),
            "evidence_files": [],
            "duplicate_file_entries": 0,
            "overlapping_source_entries": 0,
        },
    }
    _fit_budget(payload, max_chars)
    _refresh_metrics(payload)
    _fit_budget(payload, max_chars)
    _refresh_metrics(payload)
    return payload


def evaluate_prepared_plans(
    prepared: list,
    index_setups: dict[str, dict],
    manifest_records: list[dict],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict:
    """Evaluate already-prepared cases without invoking an agent or provider."""
    from code_review_ai.full_agent_eval import _case_config
    from code_review_ai.db import connect

    manifest_by_id = {record["id"]: record for record in manifest_records}
    results = []
    for item in prepared:
        setup = index_setups[item.case.case_id]
        config = _case_config(item, setup["db_path"])
        conn = connect(setup["db_path"])
        try:
            plan = plan_context(config, conn, diff=item.diff,
                                max_chars=max_chars)
        finally:
            conn.close()
        record = manifest_by_id[item.case.case_id]
        evidence_files = set(plan["metrics"]["evidence_files"])
        mutation_files = set(record.get("mutation_paths", []))
        gold_files = set(record.get("gold_files", []))
        gold_symbols = set(record.get("gold_symbols", []))
        evidence_symbols = {
            entry.get("qname") for entry in plan["evidence"]
            if entry.get("qname")
        }
        result = {
            "case_id": item.case.case_id,
            "route": plan["route"],
            "reasons": plan["reasons"],
            "serialized_chars": plan["metrics"]["serialized_chars"],
            "truncated": plan["metrics"]["truncated"],
            "evidence_files": sorted(evidence_files),
            "mutation_file_recall": _recall(evidence_files, mutation_files),
            "gold_test_file_recall": _recall(evidence_files, gold_files),
            "gold_symbol_recall": (_recall(evidence_symbols, gold_symbols)
                                   if gold_symbols else None),
            "duplicate_file_entries": plan["metrics"]["duplicate_file_entries"],
            "overlapping_source_entries": plan["metrics"].get(
                "overlapping_source_entries", 0),
            "plan": plan,
            "index": {key: setup[key] for key in
                      ("nodes", "edges", "flows", "elapsed_ms")
                      if key in setup},
        }
        if record.get("expected_route") in {"local", "graph"}:
            result["expected_route"] = record["expected_route"]
            result["route_correct"] = plan["route"] == record["expected_route"]
        results.append(result)

    route_labels = [item for item in results if "route_correct" in item]
    return {
        "schema_version": 1,
        "evaluation": "deterministic_context_planner",
        "llm_calls": 0,
        "model_cost_usd": 0.0,
        "cases": results,
        "aggregate": {
            "case_count": len(results),
            "local_routes": sum(item["route"] == "local" for item in results),
            "graph_routes": sum(item["route"] == "graph" for item in results),
            "mean_serialized_chars": _mean(
                [item["serialized_chars"] for item in results]),
            "max_serialized_chars": max(
                (item["serialized_chars"] for item in results), default=0),
            "macro_mutation_file_recall": _mean(
                [item["mutation_file_recall"] for item in results]),
            "macro_gold_test_file_recall": _mean(
                [item["gold_test_file_recall"] for item in results
                 if item["gold_test_file_recall"] is not None]),
            "route_accuracy": (_mean(
                [float(item["route_correct"]) for item in route_labels])
                if route_labels else None),
            "route_accuracy_note": (
                "not scored: manifest has no expected_route labels"
                if not route_labels else None),
        },
    }


def run_context_plan_eval(
    cases_path: str,
    repos_dir: str,
    work_dir: str,
    *,
    case_ids: list[str] | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict:
    """Prepare cached real-repo cases, index them and evaluate locally.

    Unlike the online evaluator this entry point never clones.  Requiring the
    repository cache up front makes the no-network/no-provider contract
    explicit and reproducible.
    """
    from code_review_ai.full_agent_eval import (
        _prepare_case_index,
        load_full_agent_cases,
        prepare_full_agent_cases,
        select_full_agent_cases,
    )

    manifest = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    if not isinstance(manifest, list):
        raise ValueError("context plan manifest must be a JSON array")
    cases = select_full_agent_cases(load_full_agent_cases(cases_path), case_ids)
    selected_ids = {case.case_id for case in cases}
    records = [record for record in manifest if record.get("id") in selected_ids]
    cache_root = Path(repos_dir).resolve()
    missing = [case.repo_name for case in cases
               if not (cache_root / case.repo_name / ".git").exists()]
    if missing:
        raise ValueError(
            "local context-plan eval requires cached repositories; missing: "
            + ", ".join(sorted(set(missing))))
    prepared = prepare_full_agent_cases(cases, repos_dir, work_dir)
    setups = {
        item.case.case_id: _prepare_case_index(item, work_dir, "context-plan")
        for item in prepared
    }
    return evaluate_prepared_plans(
        prepared, setups, records, max_chars=max_chars)


def _git_diff(config: Config, files: list[str] | None) -> str:
    args = ["git", "diff", "--no-ext-diff", "--unified=3",
            _resolve_diff_base(config)]
    if files:
        args += ["--", *files]
    run = subprocess.run(args, cwd=config.repo_path, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    if run.returncode:
        raise RuntimeError(
            f"git diff failed (exit {run.returncode}): {run.stderr.strip()}")
    return run.stdout


def _change_records(summary: dict) -> list[dict]:
    records = [*summary.get("changed_functions", []),
               *summary.get("delete_change", [])]
    seen: set[tuple] = set()
    result = []
    for record in records:
        key = (record.get("qname"), record.get("file"),
               record.get("start_line"), record.get("end_line"))
        if key not in seen:
            seen.add(key)
            result.append(record)
    return result


def _record_files(records: list[dict], summary: dict) -> set[str]:
    files = {record["file"] for record in records if record.get("file")}
    files.update(change["file"] for change in summary.get("uncovered_changes", [])
                 if change.get("file"))
    return files


def _choose_route(conn: sqlite3.Connection, records: list[dict],
                  summary: dict, diff: str) -> tuple[str, list[str], dict]:
    reasons: list[str] = []
    files = _record_files(records, summary)
    leaf_records = [record for record in records if record.get("kind") != "class"]
    class_records = [record for record in records if record.get("kind") == "class"]
    # A class node contains every changed method, so counting both would make
    # practically every object-oriented edit look non-local.  Keep class nodes
    # only when the changed hunk is at class scope rather than in a method.
    class_scope = _class_scope_changes(diff, class_records, leaf_records)
    routing_records = [*leaf_records, *class_scope]
    symbols = [record["qname"] for record in routing_records
               if record.get("qname")]
    deleted = summary.get("delete_change", [])
    uncovered = summary.get("uncovered_changes", [])

    if deleted:
        reasons.append("deleted-symbol")
    if uncovered:
        reasons.append("uncovered-change")
    if len(files) > 1:
        reasons.append("multiple-change-files")
    # Multiple overlapping records may be a method plus its containing class;
    # count distinct qnames because both are meaningful contract surfaces.
    if len(set(symbols)) > 1:
        reasons.append("multiple-changed-symbols")
    if class_scope:
        reasons.append("class-scope-change")
    changed_lines = [line for line in diff.splitlines()
                     if line.startswith(("+", "-"))
                     and not line.startswith(("+++", "---"))]
    if any(_INHERITANCE_CHANGE.search(line) for line in changed_lines):
        reasons.append("inheritance-change")
    elif any(_DECLARATION_CHANGE.search(line) for line in changed_lines):
        reasons.append("declaration-or-annotation-change")

    cross_file_callers = 0
    resolved_callers = 0
    for record in routing_records:
        qname, own_file = record.get("qname"), record.get("file")
        if not qname:
            continue
        rows = conn.execute(
            "SELECT DISTINCT n.file_path,n.is_test FROM edges e "
            "JOIN nodes n ON n.qualified_name=e.source "
            "WHERE e.target=? AND e.kind='call' AND e.resolution='resolved'",
            (qname,),
        ).fetchall()
        resolved_callers += len(rows)
        for row in rows:
            if not row["is_test"] and own_file and not _same_file(
                    row["file_path"], own_file):
                cross_file_callers += 1
    if cross_file_callers:
        reasons.append("cross-file-production-callers")

    reasons = list(dict.fromkeys(reasons))
    stats = {
        "changed_symbols": len(set(symbols)),
        "resolved_callers": resolved_callers,
        "cross_file_production_callers": cross_file_callers,
    }
    return ("graph" if reasons else "local"), reasons, stats


def _class_scope_changes(diff: str, classes: list[dict],
                         leaves: list[dict]) -> list[dict]:
    hunks = _diff_hunks_by_file(diff)
    result = []
    for record in classes:
        file = record.get("file")
        if not file:
            continue
        leaf_ranges = [
            (leaf.get("start_line") or 0, leaf.get("end_line") or 0)
            for leaf in leaves if leaf.get("file") == file
        ]
        for start, count in hunks.get(file, []):
            end = start + count - 1
            if not any(not (end < leaf_start or start > leaf_end)
                       for leaf_start, leaf_end in leaf_ranges):
                result.append(record)
                break
    return result


def _diff_hunks_by_file(diff: str) -> dict[str, list[tuple[int, int]]]:
    result: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in diff.splitlines():
        file_match = re.match(r"^\+\+\+ b/(.+)$", line)
        if file_match:
            current = file_match.group(1)
            result.setdefault(current, [])
            continue
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if current and hunk:
            start = max(1, int(hunk.group(1)))
            count = max(1, int(hunk.group(2) or 1))
            result[current].append((start, count))
    return result


def _same_file(absolute_or_relative: str | None, relative: str) -> bool:
    if not absolute_or_relative:
        return False
    return absolute_or_relative.replace("\\", "/").endswith(relative.replace("\\", "/"))


def _changed_source(config: Config, records: list[dict]) -> list[dict]:
    # Prefer the narrow method/function record over an overlapping class body.
    ordered = sorted(records, key=lambda item: (
        item.get("kind") == "class",
        (item.get("end_line") or 0) - (item.get("start_line") or 0),
    ))
    selected: list[dict] = []
    covered: list[tuple[str, int, int]] = []
    for record in ordered:
        file = record.get("file")
        start, end = record.get("start_line") or 0, record.get("end_line") or 0
        if not file or start < 1 or end < start:
            continue
        if record.get("kind") == "class" and any(
                path == file and start <= inner_start <= inner_end <= end
                for path, inner_start, inner_end in covered):
            continue
        item = _source_entry(config, record.get("qname"), file, start, end,
                             kind="changed-source", priority=90, max_lines=60)
        if item:
            selected.append(item)
            covered.append((file, start, end))
    return selected


def _uncovered_evidence(summary: dict) -> list[dict]:
    """Keep unsupported/deleted files visible even when no live node exists."""
    result = []
    for change in summary.get("uncovered_changes", []):
        if not change.get("file"):
            continue
        result.append({
            "kind": "deleted-file" if change.get("deleted") else "uncovered-change",
            "file": change["file"],
            "priority": 95,
            "hunks": change.get("hunks", []),
        })
    return result


def _graph_neighbors(config: Config, conn: sqlite3.Connection,
                     records: list[dict], limit: int) -> list[dict]:
    candidates: list[tuple[int, str, sqlite3.Row]] = []
    changed = {record.get("qname") for record in records}
    for record in records:
        qname = record.get("qname")
        if not qname:
            continue
        incoming = conn.execute(
            "SELECT n.qualified_name,n.file_path,n.start_line,n.end_line,n.is_test "
            "FROM edges e JOIN nodes n ON n.qualified_name=e.source "
            "WHERE e.target=? AND e.kind='call' AND e.resolution='resolved' "
            "ORDER BY n.is_test DESC,n.qualified_name", (qname,)).fetchall()
        outgoing = conn.execute(
            "SELECT n.qualified_name,n.file_path,n.start_line,n.end_line,n.is_test "
            "FROM edges e JOIN nodes n ON n.qualified_name=e.target "
            "WHERE e.source=? AND e.kind='call' AND e.resolution='resolved' "
            "ORDER BY n.is_test DESC,n.qualified_name", (qname,)).fetchall()
        candidates.extend((75 if row["is_test"] else 80, "caller", row)
                          for row in incoming)
        candidates.extend((65, "callee", row) for row in outgoing)
    result = []
    seen = set(changed)
    seen_test_files: set[str] = set()
    for priority, kind, row in sorted(candidates, key=lambda item: -item[0]):
        qname = row["qualified_name"]
        if qname in seen:
            continue
        file = _relative(config, row["file_path"])
        if row["is_test"] and file in seen_test_files:
            continue
        seen.add(qname)
        if row["is_test"]:
            seen_test_files.add(file)
        entry = _source_entry(
            config, qname, file, row["start_line"], row["end_line"],
            kind="test" if row["is_test"] else kind,
            priority=priority, max_lines=20,
        )
        if entry:
            result.append(entry)
        if len(result) >= limit:
            break
    return result


def _target_tests(config: Config, conn: sqlite3.Connection,
                  records: list[dict], limit: int) -> list[dict]:
    if not limit:
        return []
    terms = _change_terms(records)
    names = _change_names(records)
    if not terms and not names:
        return []
    rows = conn.execute(
        "SELECT qualified_name,file_path,start_line,end_line FROM nodes "
        "WHERE is_test=1 ORDER BY qualified_name"
    ).fetchall()
    scored = []
    for row in rows:
        file_stem = Path(row["file_path"]).stem.lower().removesuffix("test")
        haystack = f"{row['qualified_name']} {row['file_path']}".lower()
        prefix = max((_common_prefix(name, file_stem) for name in names), default=0)
        # A domain prefix such as Map* is much more discriminating than
        # generic suffixes such as Adapter/Factory.  Exact/long class-name
        # overlap naturally wins for GraphAdapterBuilder and runtime wrappers.
        score = (20 + prefix if prefix >= 3 else 0) + sum(
            3 if term in Path(row["file_path"]).name.lower() else 1
            for term in terms if term in haystack)
        if score:
            scored.append((score, row))
    result = []
    used_files: set[str] = set()
    for _, row in sorted(scored, key=lambda item: (-item[0], item[1]["qualified_name"])):
        file = _relative(config, row["file_path"])
        if file in used_files:
            continue
        entry = _source_entry(config, row["qualified_name"], file,
                              row["start_line"], row["end_line"],
                              kind="test", priority=75, max_lines=24)
        if entry:
            result.append(entry)
            used_files.add(file)
        if len(result) >= limit:
            break
    return result


def _change_terms(records: list[dict]) -> set[str]:
    terms: set[str] = set()
    for record in records:
        simple = (record.get("qname") or "").replace("::", ".").split(".")[-1]
        for token in _CAMEL_TOKEN.findall(simple):
            token = token.lower()
            if len(token) >= 3 and token not in {
                    "java", "test", "type", "adapter", "factory", "builder"}:
                terms.add(token)
        if len(simple) >= 5:
            terms.add(simple.lower())
    return terms


def _change_names(records: list[dict]) -> set[str]:
    return {
        (record.get("qname") or "").replace("::", ".").split(".")[-1].lower()
        for record in records if record.get("kind") == "class"
    }


def _common_prefix(left: str, right: str) -> int:
    count = 0
    for a, b in zip(left, right):
        if a != b:
            break
        count += 1
    return count


def _source_entry(config: Config, qname: str | None, file: str,
                  start: int, end: int, *, kind: str, priority: int,
                  max_lines: int) -> dict | None:
    path = Path(config.repo_path) / file
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    start = max(1, start)
    clipped_end = min(end, start + max_lines - 1, len(lines))
    if clipped_end < start:
        return None
    source = "\n".join(
        f"{line_no}: {lines[line_no - 1]}"
        for line_no in range(start, clipped_end + 1)
    )
    return {
        "kind": kind, "qname": qname, "file": file,
        "start_line": start, "end_line": clipped_end,
        "truncated": clipped_end < end,
        "priority": priority, "source": source,
    }


def _relative(config: Config, path: str) -> str:
    try:
        return Path(path).resolve().relative_to(
            Path(config.repo_path).resolve()).as_posix()
    except ValueError:
        return path.replace("\\", "/")


def _fit_budget(payload: dict, max_chars: int) -> None:
    def size() -> int:
        return len(json.dumps(payload, ensure_ascii=False))

    while size() > max_chars:
        evidence = payload["evidence"]
        # Source bodies from selected tests/neighbors are the first expendable
        # bytes; their file/qname still preserve retrieval evidence.
        sourced = [item for item in evidence
                   if item.get("source") and item.get("kind") != "diff"]
        if sourced:
            victim = min(sourced, key=lambda item: (
                item.get("priority", 0), -len(item.get("source", ""))))
            source = victim["source"]
            if len(source) > 500:
                victim["source"] = source[:max(250, len(source) // 2)] + "\n…"
                victim["truncated"] = True
            else:
                del victim["source"]
                victim["truncated"] = True
            payload["metrics"]["truncated"] = True
            continue
        diff_entry = next((item for item in evidence
                           if item.get("kind") == "diff"), None)
        if diff_entry and len(diff_entry.get("source", "")) > 200:
            over = size() - max_chars
            source = diff_entry["source"]
            keep = max(100, len(source) - over - 20)
            diff_entry["source"] = source[:keep] + "\n…"
            diff_entry["truncated"] = True
            payload["metrics"]["truncated"] = True
            continue
        optional = [item for item in evidence if item.get("kind") != "diff"]
        if optional:
            evidence.remove(min(optional, key=lambda item: item.get("priority", 0)))
            payload["metrics"]["truncated"] = True
            continue
        break


def _dedupe_overlapping_evidence(evidence: list[dict]) -> list[dict]:
    """Drop lower-priority excerpts which cover an already selected range."""
    selected: list[dict] = []
    ranges: dict[str, list[tuple[int, int]]] = {}
    for item in sorted(enumerate(evidence),
                       key=lambda pair: (-pair[1].get("priority", 0), pair[0])):
        _, entry = item
        file = entry.get("file")
        start, end = entry.get("start_line"), entry.get("end_line")
        if file and isinstance(start, int) and isinstance(end, int):
            if any(not (end < old_start or start > old_end)
                   for old_start, old_end in ranges.get(file, [])):
                continue
            ranges.setdefault(file, []).append((start, end))
        selected.append(entry)
    # Present evidence by utility while keeping the diff first.
    return sorted(selected, key=lambda item: -item.get("priority", 0))


def _refresh_metrics(payload: dict) -> None:
    files = [item["file"] for item in payload["evidence"] if item.get("file")]
    overlap_count = 0
    ranges: dict[str, list[tuple[int, int]]] = {}
    for item in payload["evidence"]:
        file = item.get("file")
        start, end = item.get("start_line"), item.get("end_line")
        if not file or not isinstance(start, int) or not isinstance(end, int):
            continue
        if any(not (end < old_start or start > old_end)
               for old_start, old_end in ranges.get(file, [])):
            overlap_count += 1
        ranges.setdefault(file, []).append((start, end))
    payload["metrics"].update({
        "evidence_entries": len(payload["evidence"]),
        "evidence_files": sorted(set(files)),
        "duplicate_file_entries": len(files) - len(set(files)),
        "overlapping_source_entries": overlap_count,
    })
    payload["metrics"]["serialized_chars"] = len(
        json.dumps(payload, ensure_ascii=False))


def _recall(actual: set[str], gold: set[str]) -> float | None:
    return round(len(actual & gold) / len(gold), 4) if gold else None


def _mean(values: list[float | int]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None
