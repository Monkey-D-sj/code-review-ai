"""End-to-end evaluation of code-review agents under controlled context modes."""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from code_review_ai.config import Config
from code_review_ai.impact import get_impact
from code_review_ai.indexer import rebuild
from code_review_ai.parser import SOURCE_GLOBS, filter_excluded, list_source_files

MODES = ("diff_only", "search_baseline", "graph_agent", "hybrid_agent")
HYBRID_MAX_CHARS = 12_000
MCP_TOOL_PREFIX = "mcp__code-review-ai__"


@dataclass(frozen=True)
class GoldFinding:
    finding_id: str
    file: str
    line_start: int | None
    line_end: int | None
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class AgentEvalCase:
    case_id: str
    prompt: str
    diff: str
    changed_symbols: tuple[str, ...]
    gold_findings: tuple[GoldFinding, ...]
    source_commit: str | None


@dataclass(frozen=True)
class AgentRun:
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: float


AgentExecutor = Callable[[list[str], str, str, dict[str, str], int], AgentRun]


def load_agent_cases(path: str) -> list[AgentEvalCase]:
    """Load the JSON manifest used by ``agent-eval``."""
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(records, list) or not records:
        raise ValueError("agent eval manifest must be a non-empty JSON array")
    return [_parse_case(record, position) for position, record in enumerate(records)]


def select_agent_cases(cases: list[AgentEvalCase],
                       case_ids: list[str] | None) -> list[AgentEvalCase]:
    if not case_ids:
        return cases
    selected_ids = set(case_ids)
    selected = [case for case in cases if case.case_id in selected_ids]
    missing = selected_ids - {case.case_id for case in selected}
    if missing:
        raise ValueError(f"unknown agent eval case ids: {', '.join(sorted(missing))}")
    return selected


def _parse_case(record: object, position: int) -> AgentEvalCase:
    if not isinstance(record, dict):
        raise ValueError(f"case {position} must be an object")
    case_id = record.get("id")
    prompt = record.get("prompt")
    diff = record.get("diff", "")
    symbols = record.get("changed_symbols", [])
    golds = record.get("gold_findings")
    valid_symbols = isinstance(symbols, list) and all(
        isinstance(symbol, str) and symbol for symbol in symbols)
    if not isinstance(case_id, str) or not case_id:
        raise ValueError(f"case {position} requires a non-empty id")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError(f"case {case_id} requires a non-empty prompt")
    if not isinstance(diff, str) or not valid_symbols:
        raise ValueError(f"case {case_id} has invalid diff or changed_symbols")
    if not isinstance(golds, list) or not golds:
        raise ValueError(f"case {case_id} requires gold_findings")
    findings = tuple(_parse_gold(gold, case_id) for gold in golds)
    source_commit = record.get("source_commit")
    if source_commit is not None and (not isinstance(source_commit, str)
                                      or not source_commit):
        raise ValueError(f"case {case_id} has invalid source_commit")
    return AgentEvalCase(case_id, prompt, diff, tuple(symbols), findings,
                         source_commit)


def _parse_gold(record: object, case_id: str) -> GoldFinding:
    if not isinstance(record, dict):
        raise ValueError(f"case {case_id} has an invalid gold finding")
    finding_id = record.get("id")
    file_path = record.get("file")
    line_start = record.get("line_start")
    line_end = record.get("line_end", line_start)
    keywords = record.get("keywords", [])
    valid_lines = ((line_start is None and line_end is None) or
                   (isinstance(line_start, int) and isinstance(line_end, int)
                    and 1 <= line_start <= line_end))
    valid_keywords = isinstance(keywords, list) and all(
        isinstance(keyword, str) and keyword for keyword in keywords)
    if not isinstance(finding_id, str) or not finding_id:
        raise ValueError(f"case {case_id} gold finding requires id")
    if not isinstance(file_path, str) or not file_path or not valid_lines:
        raise ValueError(f"case {case_id} gold finding has invalid file/lines")
    if not valid_keywords:
        raise ValueError(f"case {case_id} gold finding has invalid keywords")
    return GoldFinding(finding_id, _normalize(file_path), line_start, line_end,
                       tuple(keyword.lower() for keyword in keywords))


def run_agent_eval(config: Config, conn: sqlite3.Connection,
                   cases: list[AgentEvalCase], command: list[str],
                   output_dir: str, modes: tuple[str, ...] = MODES,
                   repetitions: int = 1, timeout_seconds: int = 300,
                   workers: int = 1,
                   executor: AgentExecutor | None = None) -> dict:
    """Run every case/mode/repetition and return a machine-readable report."""
    _validate_run(cases, command, modes, repetitions, timeout_seconds, workers)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    rebuild(config, conn)
    execute = executor or _execute_agent
    jobs = []
    for case in cases:
        contexts = _case_contexts(config, conn, case)
        for mode in modes:
            for repetition in range(1, repetitions + 1):
                jobs.append((case, mode, repetition, contexts[mode]))
    results = _execute_jobs(config, jobs, command, output_dir,
                            timeout_seconds, workers, execute)
    return {
        "schema_version": 1,
        "repository": str(Path(config.repo_path).resolve()),
        "command": command,
        "modes": list(modes),
        "repetitions": repetitions,
        "workers": workers,
        "aggregate": _aggregate(results, modes),
        "runs": results,
    }


def preflight_agent_eval(config: Config, conn: sqlite3.Connection,
                         cases: list[AgentEvalCase],
                         modes: tuple[str, ...] = MODES) -> dict:
    """Build contexts and validate symbol/budget coverage without an agent call."""
    _validate_run(cases, ["preflight"], modes, 1, 1, 1)
    rebuild(config, conn)
    results: list[dict] = []
    for case in cases:
        contexts = _case_contexts(config, conn, case)
        found_symbols = _found_symbols(conn, case.changed_symbols)
        results.append({
            "case_id": case.case_id, "source_commit": case.source_commit,
            "changed_symbols": list(case.changed_symbols),
            "found_symbols": found_symbols,
            "symbol_found_rate": round(
                len(found_symbols) / len(case.changed_symbols), 4)
            if case.changed_symbols else 0.0,
            "gold_findings": len(case.gold_findings),
            "contexts": {mode: {
                "characters": len(contexts[mode]),
                "files": _context_files(config, contexts[mode]),
            } for mode in modes},
        })
    return {"schema_version": 1, "dry_run": True, "cases": results,
            "aggregate": _preflight_aggregate(results, modes)}


def _found_symbols(conn: sqlite3.Connection,
                   symbols: tuple[str, ...]) -> list[str]:
    return [symbol for symbol in symbols if conn.execute(
        "SELECT 1 FROM nodes WHERE qualified_name=?", (symbol,)).fetchone()]


def _preflight_aggregate(results: list[dict],
                         modes: tuple[str, ...]) -> dict:
    symbol_total = sum(len(result["changed_symbols"]) for result in results)
    symbol_found = sum(len(result["found_symbols"]) for result in results)
    return {
        "case_count": len(results), "symbol_count": symbol_total,
        "symbol_found_rate": round(symbol_found / symbol_total, 4)
        if symbol_total else 0.0,
        "modes": {mode: {
            "max_characters": max(
                (result["contexts"][mode]["characters"] for result in results),
                default=0),
            "mean_characters": round(sum(
                result["contexts"][mode]["characters"] for result in results
            ) / len(results), 2) if results else 0.0,
            "mean_files": round(sum(
                len(result["contexts"][mode]["files"]) for result in results
            ) / len(results), 2) if results else 0.0,
        } for mode in modes},
    }


def _validate_run(cases: list[AgentEvalCase], command: list[str],
                  modes: tuple[str, ...], repetitions: int,
                  timeout_seconds: int, workers: int) -> None:
    if not cases or not command:
        raise ValueError("cases and agent command are required")
    if not modes or any(mode not in MODES for mode in modes):
        raise ValueError(f"modes must be selected from {', '.join(MODES)}")
    if repetitions < 1 or timeout_seconds < 1 or workers < 1:
        raise ValueError("repetitions, timeout, and workers must be at least 1")


def _execute_jobs(config: Config, jobs: list[tuple], command: list[str],
                  output_dir: str, timeout_seconds: int, workers: int,
                  executor: AgentExecutor) -> list[dict]:
    def execute(job: tuple) -> dict:
        case, mode, repetition, context = job
        return _run_once(config, case, mode, repetition, context, command,
                         output_dir, timeout_seconds, executor)

    if workers == 1:
        return [execute(job) for job in jobs]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(execute, jobs))


def _case_contexts(config: Config, conn: sqlite3.Connection,
                   case: AgentEvalCase) -> dict[str, str]:
    base = f"TASK\n{case.prompt}\n\nDIFF\n{case.diff or '(not provided)'}"
    search = json.dumps(_lexical_context(config, case), ensure_ascii=False)
    graph = json.dumps(_graph_context(config, conn, case), ensure_ascii=False)
    hybrid = json.dumps(_hybrid_context(config, conn, case), ensure_ascii=False)
    return {
        "diff_only": base,
        "search_baseline": f"{base}\n\nLEXICAL SEARCH CONTEXT\n{search}",
        "graph_agent": f"{base}\n\nCODE GRAPH CONTEXT\n{graph}",
        "hybrid_agent": f"{base}\n\nHYBRID CODE CONTEXT\n{hybrid}",
    }


def _lexical_context(config: Config, case: AgentEvalCase,
                     max_hits: int = 20) -> list[dict]:
    terms = {symbol.rsplit("::", 1)[-1].rsplit(".", 1)[-1].lower()
             for symbol in case.changed_symbols}
    hits: list[dict] = []
    files = filter_excluded(list_source_files(config.repo_path, SOURCE_GLOBS),
                            config.exclude)
    for relative_path in files:
        if len(hits) >= max_hits:
            break
        path = Path(config.repo_path) / relative_path
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, 1):
            if terms and any(term in line.lower() for term in terms):
                hits.append({"file": _normalize(relative_path),
                             "line": line_number, "text": line.strip()})
                if len(hits) >= max_hits:
                    break
    return hits


def _graph_context(config: Config, conn: sqlite3.Connection,
                   case: AgentEvalCase) -> dict:
    impacts = get_impact(conn, list(case.changed_symbols),
                         max_nodes_per_direction=10, tests="exclude")
    return {"changed_symbols": list(case.changed_symbols),
            "impacts": [_compact_impact(config, impact) for impact in impacts]}


def _compact_impact(config: Config, impact: dict) -> dict:
    return {
        "symbol": impact["symbol"], "found": impact["found"],
        "affected_entries": impact["affected_entries"],
        "upstream": [_compact_node(config, node)
                     for node in impact["upstream"][:10]],
        "downstream": [_compact_node(config, node)
                       for node in impact["downstream"][:10]],
    }


def _compact_node(config: Config, node: dict) -> dict:
    return {"qname": node["qname"], "file": _relative(config, Path(node["file"])),
            "line": node["line"]}


def _hybrid_context(config: Config, conn: sqlite3.Connection,
                    case: AgentEvalCase) -> dict:
    graph = _graph_context(config, conn, case)
    changed = [_symbol_snippet(config, conn, symbol, 80)
               for symbol in case.changed_symbols]
    changed = [snippet for snippet in changed if snippet]
    neighbor_names = _direct_neighbors(conn, case.changed_symbols, per_direction=3)
    neighbors = [_symbol_snippet(config, conn, symbol, 24)
                 for symbol in neighbor_names]
    payload = {"changed_symbol_source": changed,
               "direct_neighbor_source": [item for item in neighbors if item],
               "graph_evidence": graph}
    return _trim_hybrid(payload)


def _direct_neighbors(conn: sqlite3.Connection, symbols: tuple[str, ...],
                      per_direction: int) -> list[str]:
    neighbors: list[str] = []
    for symbol in symbols:
        incoming = conn.execute(
            "SELECT DISTINCT source AS qname FROM edges WHERE target=? "
            "AND resolution='resolved' ORDER BY source LIMIT ?",
            (symbol, per_direction),
        ).fetchall()
        outgoing = conn.execute(
            "SELECT DISTINCT target AS qname FROM edges WHERE source=? "
            "AND resolution='resolved' ORDER BY target LIMIT ?",
            (symbol, per_direction),
        ).fetchall()
        neighbors.extend(row["qname"] for row in (*incoming, *outgoing))
    return list(dict.fromkeys(neighbors))


def _symbol_snippet(config: Config, conn: sqlite3.Connection, symbol: str,
                    max_lines: int) -> dict | None:
    row = conn.execute(
        "SELECT file_path,start_line,end_line FROM nodes WHERE qualified_name=?",
        (symbol,),
    ).fetchone()
    if row is None:
        return None
    try:
        lines = Path(row["file_path"]).read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    start = max(row["start_line"] - 1, 0)
    end = min(row["end_line"], start + max_lines)
    source = "\n".join(f"{line_number}: {lines[line_number - 1]}"
                       for line_number in range(start + 1, end + 1))
    return {"qname": symbol, "file": _relative(config, Path(row["file_path"])),
            "start_line": start + 1, "end_line": end, "source": source,
            "truncated": row["end_line"] > end}


def _trim_hybrid(payload: dict) -> dict:
    """Enforce a final serialized budget, dropping neighbors before graph data."""
    payload["max_chars"] = HYBRID_MAX_CHARS
    payload["serialized_chars"] = 0
    while (len(json.dumps(payload, ensure_ascii=False)) > HYBRID_MAX_CHARS
           and payload["direct_neighbor_source"]):
        payload["direct_neighbor_source"].pop()
    graph = payload["graph_evidence"]
    while len(json.dumps(payload, ensure_ascii=False)) > HYBRID_MAX_CHARS:
        trimmed = False
        for impact in graph["impacts"]:
            for direction in ("downstream", "upstream"):
                if impact[direction]:
                    impact[direction].pop()
                    trimmed = True
                    break
            if trimmed:
                break
        if not trimmed:
            trimmed = _shrink_changed_source(payload["changed_symbol_source"])
        if not trimmed:
            break
    payload["serialized_chars"] = len(json.dumps(payload, ensure_ascii=False))
    return payload


def _shrink_changed_source(snippets: list[dict]) -> bool:
    candidates = [snippet for snippet in snippets
                  if len(snippet.get("source", "")) > 500]
    if not candidates:
        return False
    snippet = max(candidates, key=lambda item: len(item["source"]))
    snippet["source"] = snippet["source"][:-500]
    snippet["truncated"] = True
    return True


def _run_once(config: Config, case: AgentEvalCase, mode: str, repetition: int,
              context: str, command: list[str], output_dir: str,
              timeout_seconds: int, executor: AgentExecutor) -> dict:
    prompt = _agent_prompt(mode, context)
    environment = {"CRAI_EVAL_MODE": mode, "CRAI_EVAL_CASE": case.case_id}
    run = executor(command, prompt, config.repo_path, environment, timeout_seconds)
    payload, parse_error = _parse_agent_output(run.stdout)
    score = _score(payload.get("findings", []), case.gold_findings)
    usage = _usage(payload, prompt, run.stdout)
    result = {
        "case_id": case.case_id, "mode": mode, "repetition": repetition,
        "source_commit": case.source_commit,
        "success": run.returncode == 0 and parse_error is None,
        "returncode": run.returncode, "elapsed_ms": round(run.elapsed_ms, 3),
        "parse_error": parse_error, **score,
        "files_read": _string_values(payload.get("files_read")),
        "tool_calls": _string_values(payload.get("tool_calls")),
        "context_files": _context_files(config, context),
        "usage": usage,
    }
    _write_transcript(output_dir, case, mode, repetition, prompt, run, payload,
                      result)
    return result


def _agent_prompt(mode: str, context: str) -> str:
    contract = {
        "findings": [{"file": "path", "line": 1, "title": "...",
                      "description": "..."}],
        "files_read": ["path"], "tool_calls": ["tool_name"],
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    return (f"You are running a controlled code-review evaluation in {mode} mode.\n"
            "Review only from the supplied context. Do not modify the repository.\n"
            "Return exactly one JSON object matching this shape:\n"
            f"{json.dumps(contract)}\n\n{context}")


def _execute_agent(command: list[str], prompt: str, cwd: str,
                   environment: dict[str, str], timeout_seconds: int) -> AgentRun:
    started = time.perf_counter()
    process_env = os.environ.copy()
    process_env.update(environment)
    try:
        completed = subprocess.run(command, input=prompt, cwd=cwd,
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   timeout=timeout_seconds, env=process_env)
        return AgentRun(completed.returncode, completed.stdout, completed.stderr,
                        (time.perf_counter() - started) * 1000)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return AgentRun(124, stdout, stderr + "\nagent eval timed out",
                        (time.perf_counter() - started) * 1000)
    except OSError as exc:
        return AgentRun(127, "", str(exc), (time.perf_counter() - started) * 1000)


def parse_agent_command(value: str) -> list[str]:
    if value.lstrip().startswith("["):
        parsed = json.loads(value)
        command = parsed if isinstance(parsed, list) else []
        if not all(isinstance(part, str) and part for part in command):
            raise ValueError("JSON agent command must be an array of strings")
    else:
        command = shlex.split(value, posix=os.name != "nt")
        if os.name == "nt":
            command = [_strip_command_quotes(part) for part in command]
    if not command:
        raise ValueError("agent command cannot be empty")
    return command


def _strip_command_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_agent_output(stdout: str) -> tuple[dict, str | None]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {}, f"invalid JSON output: {exc.msg}"
    if not isinstance(payload, dict) or not isinstance(payload.get("findings", []), list):
        return {}, "agent output must be an object with a findings array"
    return payload, None


def _score(predictions: list[object], golds: tuple[GoldFinding, ...]) -> dict:
    valid_predictions = [item for item in predictions if isinstance(item, dict)]
    # Maximum bipartite matching avoids prediction-order artifacts when a
    # multi-site fix has related gold findings in the same file.
    gold_to_prediction: dict[int, int] = {}

    def assign(prediction_index: int, seen: set[int]) -> bool:
        for gold_index, gold in enumerate(golds):
            if gold_index in seen or not _matches(
                    valid_predictions[prediction_index], gold):
                continue
            seen.add(gold_index)
            previous = gold_to_prediction.get(gold_index)
            if previous is None or assign(previous, seen):
                gold_to_prediction[gold_index] = prediction_index
                return True
        return False

    for prediction_index in range(len(valid_predictions)):
        assign(prediction_index, set())
    matches = [{"gold_id": golds[index].finding_id,
                "file": golds[index].file}
               for index in sorted(gold_to_prediction)]
    true_positives = len(matches)
    precision = true_positives / len(valid_predictions) if valid_predictions else 0.0
    recall = true_positives / len(golds) if golds else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"predicted_findings": len(valid_predictions),
            "gold_findings": len(golds), "matched_findings": matches,
            "precision": round(precision, 4), "recall": round(recall, 4),
            "f1": round(f1, 4)}


def _matches(prediction: dict, gold: GoldFinding) -> bool:
    if _normalize(str(prediction.get("file", ""))) != gold.file:
        return False
    if gold.line_start is not None:
        line = prediction.get("line")
        if not isinstance(line, int) or not gold.line_start <= line <= gold.line_end:
            return False
    if gold.keywords:
        text = f"{prediction.get('title', '')} {prediction.get('description', '')}".lower()
        if not any(keyword in text for keyword in gold.keywords):
            return False
    return True


def _usage(payload: dict, prompt: str, stdout: str) -> dict:
    supplied = payload.get("usage")
    if isinstance(supplied, dict):
        input_tokens = supplied.get("input_tokens")
        output_tokens = supplied.get("output_tokens")
        if isinstance(input_tokens, int) and isinstance(output_tokens, int):
            return {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": _integer_usage(
                    supplied, "cache_read_input_tokens"),
                "cache_creation_input_tokens": _integer_usage(
                    supplied, "cache_creation_input_tokens"),
                "output_tokens": output_tokens, "estimated": False,
                "total_cost_usd": _number_usage(supplied, "total_cost_usd"),
                "model": supplied.get("model")
                if isinstance(supplied.get("model"), str) else None,
            }
    return {"input_tokens": _estimate_tokens(prompt),
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": _estimate_tokens(stdout), "estimated": True}


def _integer_usage(usage: dict, key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) else 0


def _number_usage(usage: dict, key: str) -> float | None:
    value = usage.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / 4)) if text else 0


def _aggregate(results: list[dict], modes: tuple[str, ...]) -> dict:
    return {mode: _mode_metrics([result for result in results
                                if result["mode"] == mode]) for mode in modes}


def _mode_metrics(results: list[dict]) -> dict:
    count = len(results)
    return {
        "runs": count,
        "success_rate": _mean(results, lambda result: float(result["success"])),
        "macro_precision": _mean(results, lambda result: result["precision"]),
        "macro_recall": _mean(results, lambda result: result["recall"]),
        "macro_f1": _mean(results, lambda result: result["f1"]),
        "mean_elapsed_ms": _mean(results, lambda result: result["elapsed_ms"]),
        "mean_input_tokens": _mean(results, lambda result: result["usage"]["input_tokens"]),
        "mean_cache_read_input_tokens": _mean(
            results, lambda result: result["usage"]["cache_read_input_tokens"]),
        "mean_cache_creation_input_tokens": _mean(
            results, lambda result: result["usage"]["cache_creation_input_tokens"]),
        "mean_output_tokens": _mean(results, lambda result: result["usage"]["output_tokens"]),
        "total_cost_usd": round(sum(
            result["usage"].get("total_cost_usd") or 0.0 for result in results), 6),
        "mean_files_read": _mean(results, lambda result: len(result["files_read"])),
        "mean_context_files": _mean(results, lambda result: len(result["context_files"])),
        "mean_tool_calls": _mean(results, lambda result: len(result["tool_calls"])),
    }


def _mean(results: list[dict], getter: Callable[[dict], float]) -> float:
    return round(sum(getter(result) for result in results) / len(results), 4) if results else 0.0


def _write_transcript(output_dir: str, case: AgentEvalCase, mode: str,
                      repetition: int, prompt: str, run: AgentRun,
                      payload: dict, result: dict) -> None:
    path = Path(output_dir) / case.case_id / mode
    path.mkdir(parents=True, exist_ok=True)
    record = {"prompt": prompt, "stdout": run.stdout, "stderr": run.stderr,
              "parsed_output": payload, "result": result}
    (path / f"run-{repetition}.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _relative(config: Config, path: Path) -> str:
    try:
        return _normalize(str(path.resolve().relative_to(Path(config.repo_path).resolve())))
    except ValueError:
        return _normalize(str(path))


def _normalize(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")


_SOURCE_PATH_RE = re.compile(
    r"(?:[A-Za-z]:)?[A-Za-z0-9_./\\-]+\.(?:py|ts|tsx|js|jsx|vue|java)"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def _context_files(config: Config, context: str) -> list[str]:
    files: list[str] = []
    for match in _SOURCE_PATH_RE.findall(context):
        normalized = _normalize(match).removeprefix("a/").removeprefix("b/")
        path = Path(normalized)
        if path.is_absolute():
            normalized = _relative(config, path)
        files.append(normalized)
    return list(dict.fromkeys(files))


def _string_values(value: object) -> list[str]:
    return list(dict.fromkeys(item for item in value
                              if isinstance(item, str))) if isinstance(value, list) else []
