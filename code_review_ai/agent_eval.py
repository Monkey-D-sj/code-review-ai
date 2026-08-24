"""End-to-end evaluation of code-review agents under controlled context modes."""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from code_review_ai.changes import detect_changed_symbols
from code_review_ai.config import Config, load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.impact import get_impact
from code_review_ai.indexer import rebuild
from code_review_ai.parser import SOURCE_GLOBS, filter_excluded, list_source_files

MODES = ("diff_only", "search_baseline", "graph_agent", "hybrid_agent")
DIFFICULTIES = ("trivial", "medium", "hard")
DEFAULT_DIFFICULTY = "unclassified"
HYBRID_MAX_CHARS = 12_000
SHARED_REVIEW_POLICY = """无论有哪些上下文工具可用，都应遵循此评审策略。对于每个发生变更的符号，
先检查差异及其局部代码，然后判断该变更是否自包含。只有在不改变公共签名、
返回类型、异常行为、外部可观察语义或跨模块调用的情况下，才将注释、格式调整、
仅重命名以及函数局部实现变更视为自包含。对于每个非自包含变更，先检查上游调用方。
当参数、调用或所使用的返回值发生变化时，还要检查下游被调用方。适用时，还应检查
相关测试、配置、路由、依赖注入和公共 API 边界。利用可用上下文仅收集完成此流程
所需的证据，不要重新读取已经掌握的证据。"""
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class GoldFinding:
    finding_id: str
    file: str
    line_start: int | None
    line_end: int | None
    keywords: tuple[str, ...]
    alternate_files: tuple[str, ...] = ()
    # Minimum number of distinct keywords that must appear in the prediction
    # text. 1 = the legacy OR-match (any single keyword scores). >1 forces a
    # causal description: a finding that merely echoes a surface keyword (e.g.
    # "timeout is None") without naming the failure mechanism scores nothing.
    min_matches: int = 1


@dataclass(frozen=True)
class AgentEvalCase:
    case_id: str
    prompt: str
    diff: str
    changed_symbols: tuple[str, ...]
    gold_findings: tuple[GoldFinding, ...]
    source_commit: str | None
    repo_name: str | None = None
    repo_url: str | None = None
    mutation_paths: tuple[str, ...] = ()
    complexity_tags: tuple[str, ...] = ()
    difficulty: str = DEFAULT_DIFFICULTY


@dataclass(frozen=True)
class AgentRun:
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: float


@dataclass
class _CaseSnapshot:
    config: Config
    conn: sqlite3.Connection
    source_repo: Path
    worktree: Path
    diff: str
    changed_symbols: tuple[str, ...]


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
    # ``hint`` is the current manifest spelling (see ``full_agent_eval``, which
    # keeps it out of the prompt unless the run is hinted). This harness still
    # injects the prose unconditionally — the alias only keeps a hint-only
    # manifest loadable here.
    prompt = record.get("prompt", record.get("hint"))
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
    repo_name = record.get("repo_name")
    repo_url = record.get("repo_url")
    mutation_paths = record.get("mutation_paths", [])
    complexity_tags = record.get("complexity_tags", [])
    difficulty = record.get("difficulty", DEFAULT_DIFFICULTY)
    if not isinstance(complexity_tags, list) or not all(
            isinstance(tag, str) and tag for tag in complexity_tags):
        raise ValueError(f"case {case_id} has invalid complexity_tags")
    if difficulty not in (*DIFFICULTIES, DEFAULT_DIFFICULTY):
        raise ValueError(
            f"case {case_id} has invalid difficulty; expected one of "
            f"{', '.join(DIFFICULTIES)}")
    external_fields = (repo_name, repo_url, mutation_paths)
    is_external = any(value not in (None, []) for value in external_fields)
    if is_external:
        valid_paths = isinstance(mutation_paths, list) and mutation_paths and all(
            isinstance(path, str) and path and not Path(path).is_absolute()
            and ".." not in Path(path).parts for path in mutation_paths)
        if not isinstance(repo_name, str) or not _SAFE_NAME.match(repo_name):
            raise ValueError(f"case {case_id} has invalid repo_name")
        if not isinstance(repo_url, str) or not repo_url.startswith("https://"):
            raise ValueError(f"case {case_id} requires an HTTPS repo_url")
        if source_commit is None:
            raise ValueError(f"case {case_id} requires source_commit")
        if not valid_paths:
            raise ValueError(f"case {case_id} has invalid mutation_paths")
    return AgentEvalCase(
        case_id, prompt, diff, tuple(symbols), findings, source_commit,
        repo_name if is_external else None, repo_url if is_external else None,
        tuple(mutation_paths) if is_external else (),
        tuple(complexity_tags), difficulty,
    )


def _parse_gold(record: object, case_id: str) -> GoldFinding:
    if not isinstance(record, dict):
        raise ValueError(f"case {case_id} has an invalid gold finding")
    finding_id = record.get("id")
    file_path = record.get("file")
    line_start = record.get("line_start")
    line_end = record.get("line_end", line_start)
    keywords = record.get("keywords", [])
    alternate_files = record.get("alternate_files", [])
    min_matches = record.get("min_matches", 1)
    valid_lines = ((line_start is None and line_end is None) or
                   (isinstance(line_start, int) and isinstance(line_end, int)
                    and 1 <= line_start <= line_end))
    valid_keywords = isinstance(keywords, list) and all(
        isinstance(keyword, str) and keyword for keyword in keywords)
    valid_alternates = isinstance(alternate_files, list) and all(
        isinstance(path, str) and path and not Path(path).is_absolute()
        and ".." not in Path(path).parts for path in alternate_files)
    valid_min_matches = (isinstance(min_matches, int) and min_matches >= 1
                         and min_matches <= len(keywords))
    if not isinstance(finding_id, str) or not finding_id:
        raise ValueError(f"case {case_id} gold finding requires id")
    if not isinstance(file_path, str) or not file_path or not valid_lines:
        raise ValueError(f"case {case_id} gold finding has invalid file/lines")
    if not valid_keywords or not valid_alternates or not valid_min_matches:
        raise ValueError(f"case {case_id} gold finding has invalid keywords")
    return GoldFinding(finding_id, _normalize(file_path), line_start, line_end,
                       tuple(keyword.lower() for keyword in keywords),
                       tuple(_normalize(path) for path in alternate_files),
                       min_matches=min_matches)


def run_agent_eval(config: Config, conn: sqlite3.Connection,
                   cases: list[AgentEvalCase], command: list[str],
                   output_dir: str, modes: tuple[str, ...] = MODES,
                   repetitions: int = 1, timeout_seconds: int = 300,
                   workers: int = 1,
                   repos_dir: str = ".code-review-ai/external-repos",
                   executor: AgentExecutor | None = None) -> dict:
    """Run every case/mode/repetition and return a machine-readable report."""
    _validate_run(cases, command, modes, repetitions, timeout_seconds, workers)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    execute = executor or _execute_agent
    jobs = []
    snapshots: list[_CaseSnapshot] = []
    try:
        if any(case.source_commit is None for case in cases):
            rebuild(config, conn)
        for case in cases:
            case_config, case_conn = config, conn
            context_case = case
            if case.source_commit is not None:
                snapshot = _create_case_snapshot(
                    config, case, Path(output_dir) / ".case-snapshots",
                    repos_dir=repos_dir)
                snapshots.append(snapshot)
                case_config, case_conn = snapshot.config, snapshot.conn
                context_case = replace(
                    case, diff=snapshot.diff,
                    changed_symbols=snapshot.changed_symbols)
            contexts = _case_contexts(case_config, case_conn, context_case)
            for mode in modes:
                for repetition in range(1, repetitions + 1):
                    jobs.append((context_case, mode, repetition, contexts[mode],
                                  case_config))
        results = _execute_jobs(jobs, command, output_dir, timeout_seconds,
                                workers, execute)
    finally:
        for snapshot in reversed(snapshots):
            _remove_case_snapshot(snapshot)
    return {
        "schema_version": 1,
        "repository": str(Path(config.repo_path).resolve())
        if not any(case.repo_url for case in cases) else None,
        "repositories": sorted({case.repo_name for case in cases
                                if case.repo_name}),
        "command": command,
        "modes": list(modes),
        "repetitions": repetitions,
        "workers": workers,
        "aggregate": _aggregate(results, modes),
        "runs": results,
    }


def preflight_agent_eval(config: Config, conn: sqlite3.Connection,
                         cases: list[AgentEvalCase],
                         modes: tuple[str, ...] = MODES,
                         repos_dir: str = ".code-review-ai/external-repos") -> dict:
    """Build contexts and validate symbol/budget coverage without an agent call."""
    _validate_run(cases, ["preflight"], modes, 1, 1, 1)
    results: list[dict] = []
    snapshots: list[_CaseSnapshot] = []
    try:
        if any(case.source_commit is None for case in cases):
            rebuild(config, conn)
        for case in cases:
            case_config, case_conn = config, conn
            context_case = case
            if case.source_commit is not None:
                snapshot = _create_case_snapshot(
                    config, case, Path(config.db_path).resolve().parent /
                    ".agent-eval-snapshots", repos_dir=repos_dir)
                snapshots.append(snapshot)
                case_config, case_conn = snapshot.config, snapshot.conn
                context_case = replace(
                    case, diff=snapshot.diff,
                    changed_symbols=snapshot.changed_symbols)
            contexts = _case_contexts(case_config, case_conn, context_case)
            found_symbols = _found_symbols(
                case_conn, context_case.changed_symbols)
            results.append({
                "case_id": case.case_id, "source_commit": case.source_commit,
                "repo_name": case.repo_name,
                "complexity_tags": list(case.complexity_tags),
                "difficulty": case.difficulty,
                "changed_symbols": list(context_case.changed_symbols),
                "found_symbols": found_symbols,
                "symbol_found_rate": round(
                    len(found_symbols) / len(context_case.changed_symbols), 4)
                if context_case.changed_symbols else 0.0,
                "gold_findings": len(case.gold_findings),
                "contexts": {mode: {
                    "characters": len(contexts[mode]),
                    "files": _context_files(case_config, contexts[mode]),
                } for mode in modes},
            })
    finally:
        for snapshot in reversed(snapshots):
            _remove_case_snapshot(snapshot)
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


def _execute_jobs(jobs: list[tuple], command: list[str], output_dir: str,
                  timeout_seconds: int, workers: int,
                  executor: AgentExecutor) -> list[dict]:
    def execute(job: tuple) -> dict:
        case, mode, repetition, context, case_config = job
        return _run_once(case_config, case, mode, repetition, context, command,
                         output_dir, timeout_seconds, executor)

    if workers == 1:
        return [execute(job) for job in jobs]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(execute, jobs))


def _create_case_snapshot(config: Config, case: AgentEvalCase,
                          work_root: Path,
                          repos_dir: str = ".code-review-ai/external-repos"
                          ) -> _CaseSnapshot:
    """Materialize a reverse mutation from a historical fix commit."""
    if case.source_commit is None:
        raise ValueError(f"case {case.case_id} has no source_commit")
    source_repo = _case_source_repository(config, case, repos_dir)
    if not (source_repo / ".git").exists():
        raise ValueError(f"source_commit requires a git repository: {source_repo}")
    suffix = uuid.uuid4().hex[:12]
    worktree = work_root.resolve() / "worktrees" / suffix
    db_path = work_root.resolve() / "indexes" / f"{suffix}.db"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _run_git(["-C", str(source_repo), "cat-file", "-e",
              f"{case.source_commit}^{{commit}}"])
    added = False
    case_conn: sqlite3.Connection | None = None
    try:
        _run_git(["-C", str(source_repo), "worktree", "add", "--detach",
                  str(worktree), case.source_commit])
        added = True
        if case.repo_url:
            mutation_paths = list(case.mutation_paths)
            for mutation_path in mutation_paths:
                _restore_parent_version(
                    worktree, case.source_commit, mutation_path)
        else:
            mutation_specs = _review_diff_specs(case.diff)
            mutation_paths = list(mutation_specs)
            _reverse_fix_hunks(worktree, source_repo, case.source_commit,
                               mutation_specs)
        actual_diff = _run_git(
            ["-C", str(worktree), "diff", "--no-ext-diff", "--unified=3",
             "--", *mutation_paths]).stdout
        if not actual_diff.strip():
            raise ValueError(f"case {case.case_id} produced an empty mutation")
        if case.repo_url:
            case_config = load_config(str(worktree))
            case_config.repo_path = str(worktree)
            case_config.db_path = str(db_path)
            case_config.diff_base = "HEAD"
            case_config.community_detection = False
        else:
            case_config = replace(config, repo_path=str(worktree),
                                  db_path=str(db_path), diff_base="HEAD")
        case_conn = connect(str(db_path))
        init_schema(case_conn)
        rebuild(case_config, case_conn)
        changed_symbols = tuple(case.changed_symbols)
        if case.repo_url:
            changed_symbols = tuple(detect_changed_symbols(
                case_config, files=mutation_paths))
        return _CaseSnapshot(case_config, case_conn, source_repo, worktree,
                             actual_diff, changed_symbols)
    except Exception:
        if case_conn is not None:
            case_conn.close()
        if added:
            _run_git(["-C", str(source_repo), "worktree", "remove", "--force",
                      str(worktree)], check=False)
        raise


def _remove_case_snapshot(snapshot: _CaseSnapshot) -> None:
    snapshot.conn.close()
    _run_git(["-C", str(snapshot.source_repo), "worktree", "remove", "--force",
              str(snapshot.worktree)], check=False)


def _case_source_repository(config: Config, case: AgentEvalCase,
                            repos_dir: str) -> Path:
    if not case.repo_url:
        return Path(config.repo_path).resolve()
    source = Path(repos_dir).resolve() / str(case.repo_name)
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        _run_git(["clone", "--filter=blob:none", case.repo_url, str(source)])
    if not (source / ".git").exists():
        raise ValueError(f"repository cache is not a git clone: {source}")
    return source


def _restore_parent_version(worktree: Path, commit: str, path: str) -> None:
    """Restore a canonical case's production path to the fix parent."""
    parent = subprocess.run(
        ["git", "-C", str(worktree), "cat-file", "-e", f"{commit}^:{path}"],
        capture_output=True,
    )
    if parent.returncode == 0:
        _run_git(["-C", str(worktree), "checkout", f"{commit}^", "--", path])
        _run_git(["-C", str(worktree), "reset", "HEAD", "--", path])
        return
    current = subprocess.run(
        ["git", "-C", str(worktree), "cat-file", "-e", f"{commit}:{path}"],
        capture_output=True,
    )
    if current.returncode != 0:
        raise ValueError(f"mutation path is absent from fix and parent: {path}")
    _run_git(["-C", str(worktree), "rm", "--force", "--", path])
    _run_git(["-C", str(worktree), "reset", "HEAD", "--", path])


def _run_git(args: list[str], stdin: str | None = None,
             check: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(["git", *args], input=stdin, capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(f"git {' '.join(args)} failed: {detail}")
    return completed


def _review_diff_specs(diff: str) -> dict[str, list[int]]:
    specs: dict[str, list[int]] = {}
    current_path: str | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git a/"):
            parts = line.split(" ")
            if len(parts) < 4 or not parts[2].startswith("a/"):
                raise ValueError(f"invalid review diff file header: {line}")
            relative = parts[2][2:]
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"unsafe review diff path: {relative}")
            specs.setdefault(relative, [])
            current_path = relative
            continue
        if line.startswith("@@ "):
            if current_path is None:
                raise ValueError("review diff hunk has no file header")
            match = re.match(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", line)
            if match is None:
                raise ValueError(f"invalid review diff hunk header: {line}")
            specs[current_path].append(int(match.group(1)))
    if not specs:
        raise ValueError("review diff contains no file paths")
    if any(not starts for starts in specs.values()):
        raise ValueError("review diff contains a file without hunks")
    return specs


def _reverse_fix_hunks(worktree: Path, source_repo: Path, commit: str,
                       specs: dict[str, list[int]]) -> None:
    fix_diff = _run_git([
        "-C", str(source_repo), "diff", f"{commit}^", commit, "--",
        *specs.keys(),
    ]).stdout
    sections = _split_git_diff(fix_diff)
    for path, requested_starts in specs.items():
        section = sections.get(path)
        if section is None:
            raise ValueError(f"fix commit does not change mutation path: {path}")
        header, hunks = section
        selected: set[int] = set()
        for requested in requested_starts:
            distances = [abs(hunk[0] - requested) for hunk in hunks]
            if not distances:
                raise ValueError(f"fix commit has no hunks for mutation path: {path}")
            best_distance = min(distances)
            best = [index for index, distance in enumerate(distances)
                    if distance == best_distance]
            if len(best) != 1:
                raise ValueError(
                    f"review hunk is ambiguous against fix commit for {path}")
            selected.add(best[0])
        patch_lines = list(header)
        for index in sorted(selected):
            patch_lines.extend(hunks[index][1])
        _run_git(["-C", str(worktree), "apply", "--reverse",
                  "--whitespace=nowarn", "-"],
                 stdin="\n".join(patch_lines) + "\n")


def _split_git_diff(diff: str) -> dict[str, tuple[list[str],
                                                   list[tuple[int, list[str]]]]]:
    sections: dict[str, tuple[list[str], list[tuple[int, list[str]]]]] = {}
    current_path: str | None = None
    header: list[str] = []
    hunks: list[tuple[int, list[str]]] = []
    current_hunk: tuple[int, list[str]] | None = None

    def flush() -> None:
        if current_path is not None:
            sections[current_path] = (list(header), list(hunks))

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            flush()
            parts = line.split(" ")
            current_path = parts[3][2:] if len(parts) >= 4 else None
            header = [line]
            hunks = []
            current_hunk = None
            continue
        if current_path is None:
            continue
        if line.startswith("@@ "):
            match = re.match(
                r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match is None:
                raise ValueError(f"invalid fix diff hunk header: {line}")
            current_hunk = (int(match.group(1)), [line])
            hunks.append(current_hunk)
        elif current_hunk is not None:
            current_hunk[1].append(line)
        else:
            header.append(line)
    flush()
    return sections


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
            "AND kind='call' AND resolution='resolved' ORDER BY source LIMIT ?",
            (symbol, per_direction),
        ).fetchall()
        outgoing = conn.execute(
            "SELECT DISTINCT target AS qname FROM edges WHERE source=? "
            "AND kind='call' AND resolution='resolved' ORDER BY target LIMIT ?",
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
        "complexity_tags": list(case.complexity_tags),
        "difficulty": case.difficulty,
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
            f"{SHARED_REVIEW_POLICY}"
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
    valid_files = (gold.file, *gold.alternate_files)
    if _normalize(str(prediction.get("file", ""))) not in valid_files:
        return False
    if gold.line_start is not None:
        line = prediction.get("line")
        if not isinstance(line, int) or not gold.line_start <= line <= gold.line_end:
            return False
    if gold.keywords:
        text = f"{prediction.get('title', '')} {prediction.get('description', '')}".lower()
        if sum(keyword in text for keyword in gold.keywords) < gold.min_matches:
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
        "mean_input_tokens": _mean(
            results, lambda result: result.get("usage", {}).get("input_tokens", 0)),
        "mean_cache_read_input_tokens": _mean(
            results, lambda result: result.get("usage", {}).get(
                "cache_read_input_tokens", 0)),
        "mean_cache_creation_input_tokens": _mean(
            results, lambda result: result.get("usage", {}).get(
                "cache_creation_input_tokens", 0)),
        "mean_output_tokens": _mean(
            results, lambda result: result.get("usage", {}).get("output_tokens", 0)),
        "total_cost_usd": round(sum(
            result.get("usage", {}).get("total_cost_usd") or 0.0
            for result in results), 6),
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
