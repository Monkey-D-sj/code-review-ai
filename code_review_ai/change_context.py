"""Compact, on-demand graph context for code review."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from code_review_ai.changes import detect_changed_symbols
from code_review_ai.config import Config
from code_review_ai.graph import VALID_DIRECTIONS, query_graph


DEFAULT_MAX_SYMBOLS = 4
DEFAULT_MAX_NEIGHBORS = 5
MAX_SYMBOLS = 8
MAX_NEIGHBORS = 8
MAX_RESULT_CHARS = 8_000
_MAX_ARGS = 6
_MAX_ARG_CHARS = 80
# Call-site snippet: ±N source lines around the call, so a reviewer can judge
# a contract change without opening the caller's file (per _attach_call_site's
# intent). Capped so the snippet never blows the 8 KB response budget.
_CALL_SITE_RADIUS = 3
_MAX_SNIPPET_CHARS = 400


def build_change_context(
    config: Config,
    conn: sqlite3.Connection,
    symbols: list[str] | None = None,
    files: list[str] | None = None,
    direction: str = "in",
    max_symbols: int = DEFAULT_MAX_SYMBOLS,
    max_neighbors: int = DEFAULT_MAX_NEIGHBORS,
    include_signatures: bool = False,
    include_tests: bool = False,
) -> dict:
    """Resolve selected changes and return a compact resolved-call neighborhood.

    This is intentionally an expansion tool, not a mandatory review bootstrap.
    The caller first decides whether the diff is non-local, then passes either
    known qnames or only the changed files that need context. Symbol discovery
    stays server-side, avoiding a separate search-symbol round trip.
    """
    if direction not in VALID_DIRECTIONS:
        raise ValueError(
            f"invalid direction {direction!r}; expected one of "
            f"{', '.join(VALID_DIRECTIONS)}"
        )
    symbol_limit = _bounded(max_symbols, MAX_SYMBOLS, "max_symbols")
    neighbor_limit = _bounded(max_neighbors, MAX_NEIGHBORS, "max_neighbors")

    if symbols is not None:
        detected = _dedupe(symbols)
        source = "symbols"
    elif files is not None and not files:
        detected = []
        source = "files"
    else:
        detected = _dedupe(detect_changed_symbols(config, files=files))
        source = "files" if files is not None else "diff"

    selected = _select_symbols(conn, detected, symbol_limit)
    contexts: list[dict] = []
    unresolved: list[str] = []
    for qname in selected:
        graph = query_graph(
            conn,
            qname,
            edge_kind="call",
            direction=direction,
            max_per_dir=neighbor_limit,
        )
        if not graph.get("found", True):
            unresolved.append(qname)
            continue
        contexts.append(
            _compact_graph(
                graph, config.repo_path, direction, include_signatures,
                include_tests,
            )
        )

    payload = {
        "changes": contexts,
        "unresolved": unresolved,
        "meta": {
            "source": source,
            "direction": direction,
            "selection_strategy": "file_diverse_call_value_v1",
            "include_tests": include_tests,
            "detected_symbols": len(detected),
            "returned_symbols": len(contexts),
            "omitted_symbols": max(0, len(detected) - len(selected)),
            "max_neighbors": neighbor_limit,
            "max_chars": MAX_RESULT_CHARS,
            "truncated": len(detected) > len(selected),
        },
    }
    _trim_to_budget(payload)
    return payload


def _bounded(value: int, ceiling: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return min(value, ceiling)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _select_symbols(
    conn: sqlite3.Connection | None, detected: list[str], limit: int
) -> list[str]:
    """Select a bounded, file-diverse set of changed symbols.

    Diff order often groups many helpers from one file. Taking its prefix can
    therefore spend the entire response budget on one implementation layer.
    We first keep the highest-value symbol from each changed file, then fill
    remaining slots by call-graph value. Stable input order breaks ties.
    """
    if len(detected) <= limit or conn is None:
        return detected[:limit]

    records = [
        _symbol_selection_record(conn, qname, index)
        for index, qname in enumerate(detected)
    ]
    by_file: dict[str, list[dict]] = {}
    for record in records:
        # Unresolved qnames retain their own bucket and can still be reported.
        file_key = record["file"] or f"<unresolved:{record['qname']}>"
        by_file.setdefault(file_key, []).append(record)

    representatives = [
        max(group, key=_selection_key) for group in by_file.values()
    ]
    # Preserve the diff's file order while it fits. When there are more files
    # than slots, prefer files whose representative has production callers.
    if len(representatives) <= limit:
        representatives.sort(key=lambda item: item["index"])
    else:
        representatives.sort(key=_selection_key, reverse=True)

    chosen = representatives[:limit]
    chosen_qnames = {item["qname"] for item in chosen}
    if len(chosen) < limit:
        remaining = [item for item in records
                     if item["qname"] not in chosen_qnames]
        remaining.sort(key=_selection_key, reverse=True)
        chosen.extend(remaining[:limit - len(chosen)])
    return [item["qname"] for item in chosen]


def _symbol_selection_record(
    conn: sqlite3.Connection, qname: str, index: int
) -> dict:
    row = conn.execute(
        "SELECT n.file_path,n.kind, "
        "COUNT(DISTINCT CASE WHEN caller.is_test=0 THEN e.source END) AS prod, "
        "COUNT(DISTINCT CASE WHEN caller.is_test=0 "
        " AND caller.file_path<>n.file_path THEN e.source END) AS cross_file "
        "FROM nodes n "
        "LEFT JOIN edges e ON e.target=n.qualified_name "
        " AND e.kind='call' AND e.resolution='resolved' "
        "LEFT JOIN nodes caller ON caller.qualified_name=e.source "
        "WHERE n.qualified_name=? "
        "GROUP BY n.file_path,n.kind",
        (qname,),
    ).fetchone()
    if row is None:
        return {"qname": qname, "file": "", "kind": "",
                "prod": 0, "cross_file": 0, "index": index}
    return {
        "qname": qname,
        "file": row["file_path"] or "",
        "kind": row["kind"] or "",
        "prod": row["prod"] or 0,
        "cross_file": row["cross_file"] or 0,
        "index": index,
    }


def _selection_key(record: dict) -> tuple[int, int, int]:
    constructor = record["qname"].endswith((".__init__", "::<init>"))
    callable_kind = record["kind"] in {"function", "method"}
    score = (record["prod"] * 10 + record["cross_file"] * 5
             + (2 if callable_kind else 0) - (8 if constructor else 0))
    return score, int(not constructor), -record["index"]


def _compact_graph(
    graph: dict, repo_root: str, direction: str, include_signatures: bool,
    include_tests: bool,
) -> dict:
    result = {
        "qname": graph["qname"],
        "kind": graph.get("kind"),
        "file": _relative(graph.get("file", ""), repo_root),
        "line": graph.get("line", 0),
    }
    if include_signatures and graph.get("signature"):
        result["signature"] = graph["signature"]
    if direction in ("in", "both"):
        result["callers"] = [
            _compact_neighbor(item, repo_root, include_signatures)
            for item in graph.get("in", [])
            if include_tests or not item.get("is_test", False)
        ]
    if direction in ("out", "both"):
        result["callees"] = [
            _compact_neighbor(item, repo_root, include_signatures)
            for item in graph.get("out", [])
        ]
    return result


def _compact_neighbor(item: dict, repo_root: str, include_signature: bool) -> dict:
    result = {
        "qname": item.get("qname"),
        "kind": item.get("kind"),
        "file": _relative(item.get("file", ""), repo_root),
        "line": item.get("line", 0),
    }
    if include_signature and item.get("signature"):
        result["signature"] = item["signature"]
    call_site = item.get("call_site")
    if isinstance(call_site, dict):
        compact_site = {
            key: call_site[key]
            for key in ("call_form", "line")
            if key in call_site
        }
        args = call_site.get("args")
        if isinstance(args, list):
            compact_site["args"] = [
                _clip(str(arg), _MAX_ARG_CHARS) for arg in args[:_MAX_ARGS]
            ]
        if "line" in compact_site:
            snippet = _call_site_code(item.get("file", ""), compact_site["line"])
            if snippet:
                compact_site["code"] = _clip(snippet, _MAX_SNIPPET_CHARS)
        if compact_site:
            result["call_site"] = compact_site
    return result


def _relative(path: str, repo_root: str) -> str:
    try:
        return Path(path).resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except ValueError:
        return path.replace("\\", "/")


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


def _call_site_code(file_path: str, line: int,
                    radius: int = _CALL_SITE_RADIUS) -> str | None:
    """±radius source lines around a call site. None when the file is
    unreadable, line is absent, or the line is out of range — the neighbor
    then simply omits the snippet instead of failing the whole payload."""
    if not file_path or not isinstance(line, int) or line < 1:
        return None
    try:
        lines = Path(file_path).read_text(encoding="utf-8",
                                          errors="replace").splitlines()
    except OSError:
        return None
    if line > len(lines):
        return None
    lo = max(0, line - 1 - radius)
    hi = min(len(lines), line + radius)
    return "\n".join(lines[lo:hi])


def _trim_to_budget(payload: dict) -> None:
    """Prefer callers, then roots; trim callees and tail neighbors first."""
    while len(json.dumps(payload)) > MAX_RESULT_CHARS:
        removed = False
        for key in ("callees", "callers"):
            for change in reversed(payload["changes"]):
                neighbors = change.get(key)
                if neighbors:
                    neighbors.pop()
                    removed = True
                    break
            if removed:
                break
        if not removed and len(payload["changes"]) > 1:
            payload["changes"].pop()
            payload["meta"]["omitted_symbols"] += 1
            removed = True
        if not removed:
            break
        payload["meta"]["truncated"] = True
        payload["meta"]["returned_symbols"] = len(payload["changes"])
