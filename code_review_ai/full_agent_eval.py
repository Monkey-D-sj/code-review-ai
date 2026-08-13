"""End-to-end evaluation with real repositories and real agent tool use."""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from code_review_ai.agent_eval import (
    AgentExecutor, AgentRun, GoldFinding, MCP_TOOL_PREFIX, _execute_agent,
    _mode_metrics, _parse_agent_output, _parse_gold, _score, _string_values,
    _usage,
)
from code_review_ai.changes import detect_changed_symbols
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.impact import get_impact
from code_review_ai.indexer import rebuild


FULL_EVAL_MODES = ("native_agent", "full_project_agent")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

# Shared review-methodology prefix, identical for every mode so the eval isolates
# the toolset (native vs MCP) rather than the review strategy. This is the
# self-containment decision the production review applies verbatim in
# code_review_ai.hooks._REVIEW_PROMPT (the post-commit review prompt) — keep the
# two in sync; parity is asserted in tests/test_full_agent_eval.py.
_REVIEW_PREFIX = (
    "对每个变更函数,先判断它是否自包含:只凭 diff 与该函数自身的代码,能否完整判断"
    "这次改动的正确性与影响范围;不能则检查其调用方/上下文:\n\n"
    "必须检查上下文(高风险):\n"
    "- 接口变更:函数删除了参数,或改变参数的类型/顺序/返回类型;新增必填参数。\n"
    "- 异常变更:之前返回 None / 错误码,现在改为抛异常。\n"
    "- 契约变更:同步改为异步,或改动引入了阻塞工作/锁。\n"
    "- 调用方依赖的行为:改动改变了调用方依赖的语义、新增或移除跨模块调用、重接路由/DI。\n"
    "- 被其他模块调用:函数被其他模块调用且改动可能破坏它们。\n"
    "- 动态语言:Python/JS 破坏性改动没有编译器拦截被破坏的调用方。\n"
    "- 跨服务:任何 RPC/API 改动(检查消费方是否就绪)。\n\n"
    "无需检查上下文(低风险):\n"
    "- 纯内部:只改了函数体(算法/优化/注释/改名/格式化),签名、返回类型与异常语义不变,"
    "且没有改变调用方依赖的行为。\n"
    "- 新增带默认值、保持现有行为的参数。\n"
    "- 私有/小范围:私有函数调用方少,且所有调用点已适配。\n\n"
    "拿不准 → 按「需要上下文」处理。\n"
    "检查深度:跨服务、删除的函数、被跨模块调用的接口变更需要最深检查(完整调用链与受影响入口);"
    "其他需要上下文的改动只需检查其调用方;私有/小范围改动只需读直接调用点。\n"
)


@dataclass(frozen=True)
class FullAgentCase:
    case_id: str
    repo_name: str
    repo_url: str
    source_commit: str
    mutation_paths: tuple[str, ...]
    prompt: str
    gold_findings: tuple[GoldFinding, ...]


@dataclass(frozen=True)
class PreparedCase:
    case: FullAgentCase
    repo_path: str
    diff: str


def load_full_agent_cases(path: str) -> list[FullAgentCase]:
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(records, list) or not records:
        raise ValueError("full agent eval manifest must be a non-empty JSON array")
    return [_parse_case(record, index) for index, record in enumerate(records)]


def _parse_case(record: object, position: int) -> FullAgentCase:
    if not isinstance(record, dict):
        raise ValueError(f"case {position} must be an object")
    case_id = record.get("id")
    repo_name = record.get("repo_name")
    repo_url = record.get("repo_url")
    commit = record.get("source_commit")
    paths = record.get("mutation_paths")
    prompt = record.get("prompt")
    golds = record.get("gold_findings")
    names = (case_id, repo_name)
    if not all(isinstance(value, str) and _SAFE_NAME.match(value)
               for value in names):
        raise ValueError(f"case {position} has an invalid id or repo_name")
    if not isinstance(repo_url, str) or not repo_url.startswith("https://"):
        raise ValueError(f"case {case_id} requires an HTTPS repo_url")
    if not isinstance(commit, str) or not commit:
        raise ValueError(f"case {case_id} requires source_commit")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError(f"case {case_id} requires prompt")
    if not isinstance(paths, list) or not paths or not all(
            isinstance(path, str) and path and not Path(path).is_absolute()
            and ".." not in Path(path).parts for path in paths):
        raise ValueError(f"case {case_id} has invalid mutation_paths")
    if not isinstance(golds, list) or not golds:
        raise ValueError(f"case {case_id} requires gold_findings")
    findings = tuple(_parse_gold(gold, case_id) for gold in golds)
    return FullAgentCase(case_id, repo_name, repo_url, commit, tuple(paths),
                         prompt, findings)


def select_full_agent_cases(cases: list[FullAgentCase],
                            case_ids: list[str] | None) -> list[FullAgentCase]:
    if not case_ids:
        return cases
    wanted = set(case_ids)
    selected = [case for case in cases if case.case_id in wanted]
    missing = wanted - {case.case_id for case in selected}
    if missing:
        raise ValueError(f"unknown full eval case ids: {', '.join(sorted(missing))}")
    return selected


def prepare_full_agent_cases(cases: list[FullAgentCase], repos_dir: str,
                             work_dir: str) -> list[PreparedCase]:
    """Clone/cache sources and reverse real fixes in isolated git worktrees."""
    repo_root = Path(repos_dir).resolve()
    work_root = Path(work_dir).resolve() / "worktrees"
    repo_root.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    prepared = []
    for case in cases:
        source = repo_root / case.repo_name
        if not source.exists():
            _run_git(["clone", "--filter=blob:none", case.repo_url, str(source)])
        if not (source / ".git").exists():
            raise ValueError(f"repository cache is not a git clone: {source}")
        _run_git(["-C", str(source), "cat-file", "-e",
                  f"{case.source_commit}^{{commit}}"])
        suffix = uuid.uuid4().hex[:8]
        worktree = work_root / f"{case.case_id}-{suffix}"
        _run_git(["-C", str(source), "worktree", "add", "--detach",
                  str(worktree), case.source_commit])
        for mutation_path in case.mutation_paths:
            _restore_parent_version(worktree, case.source_commit, mutation_path)
        diff = _run_git(["-C", str(worktree), "diff", "--no-ext-diff",
                         "--unified=40", "--", *case.mutation_paths]).stdout
        if not diff.strip():
            raise ValueError(f"case {case.case_id} produced an empty mutation")
        prepared.append(PreparedCase(case, str(worktree), diff))
    return prepared


def preflight_full_agent_eval(cases: list[FullAgentCase], repos_dir: str,
                              work_dir: str) -> dict:
    prepared = prepare_full_agent_cases(cases, repos_dir, work_dir)
    results = []
    for item in prepared:
        db_path = Path(work_dir).resolve() / "indexes" / item.case.case_id / "preflight.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        config = load_config(item.repo_path)
        config.repo_path = item.repo_path
        config.db_path = str(db_path)
        config.diff_base = "HEAD"
        config.community_detection = False
        conn = connect(str(db_path))
        init_schema(conn)
        started = time.perf_counter()
        stats = rebuild(config, conn)
        symbols = detect_changed_symbols(config)
        impacts = get_impact(conn, symbols, tests="include")
        results.append({
            "case_id": item.case.case_id, "repo_name": item.case.repo_name,
            "source_commit": item.case.source_commit,
            "repo_path": item.repo_path, "diff_characters": len(item.diff),
            "changed_symbols": symbols,
            "found_symbols": [impact["symbol"] for impact in impacts
                              if impact["found"]],
            "index": {"nodes": stats.node_count, "edges": stats.edge_count,
                      "flows": stats.flow_count,
                      "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)},
        })
        conn.close()
    total_symbols = sum(len(result["changed_symbols"]) for result in results)
    found = sum(len(result["found_symbols"]) for result in results)
    return {"schema_version": 1, "dry_run": True, "cases": results,
            "aggregate": {"case_count": len(results),
                          "changed_symbols": total_symbols,
                          "symbol_found_rate": round(found / total_symbols, 4)
                          if total_symbols else 0.0}}


def run_full_agent_eval(cases: list[FullAgentCase], repos_dir: str,
                        work_dir: str, command: list[str],
                        modes: tuple[str, ...] = FULL_EVAL_MODES,
                        repetitions: int = 1, timeout_seconds: int = 600,
                        workers: int = 1,
                        executor: AgentExecutor | None = None) -> dict:
    _validate(cases, command, modes, repetitions, timeout_seconds, workers)
    prepared = prepare_full_agent_cases(cases, repos_dir, work_dir)
    execute_agent = executor or _execute_agent
    jobs = [(item, mode, repetition) for item in prepared for mode in modes
            for repetition in range(1, repetitions + 1)]

    def execute(job: tuple[PreparedCase, str, int]) -> dict:
        item, mode, repetition = job
        return _run_once(item, mode, repetition, command, work_dir,
                         timeout_seconds, execute_agent)

    if workers == 1:
        runs = [execute(job) for job in jobs]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            runs = list(pool.map(execute, jobs))
    aggregates = _full_aggregates(runs, modes)
    return {"schema_version": 1, "evaluation": "full_project_tool_use",
            "baseline_mode": "native_agent",
            "modes": list(modes), "repetitions": repetitions,
            "workers": workers, "aggregate": aggregates, "runs": runs,
            "cases": [{"id": item.case.case_id,
                       "repo_name": item.case.repo_name,
                       "repo_url": item.case.repo_url,
                       "source_commit": item.case.source_commit,
                       "repo_path": item.repo_path} for item in prepared]}


def rescore_full_agent_report(report_path: str, cases: list[FullAgentCase],
                              transcripts_dir: str) -> dict:
    """Re-score stored model outputs after an evidence-backed gold correction."""
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    runs = report.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("full agent report has no runs")
    by_id = {case.case_id: case for case in cases}
    for run in runs:
        case_id = run.get("case_id")
        mode = run.get("mode")
        repetition = run.get("repetition")
        if case_id not in by_id or not isinstance(mode, str) \
                or not isinstance(repetition, int):
            raise ValueError(f"report contains an unknown or invalid run: {case_id}")
        transcript_path = (Path(transcripts_dir) / case_id / mode /
                           f"run-{repetition}.json")
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        payload = transcript.get("parsed_output")
        predictions = payload.get("findings", []) if isinstance(payload, dict) else []
        run.update(_score(predictions, by_id[case_id].gold_findings))
    modes = tuple(report.get("modes") or dict.fromkeys(
        run["mode"] for run in runs))
    report["aggregate"] = _full_aggregates(runs, modes)
    report["rescored"] = {
        "gold_case_count": len(cases),
        "gold_finding_count": sum(len(case.gold_findings) for case in cases),
        "method": "stored structured outputs; no provider calls rerun",
    }
    return report


def _full_aggregates(runs: list[dict], modes: tuple[str, ...]) -> dict:
    aggregates = {mode: _mode_metrics(
        [run for run in runs if run["mode"] == mode]) for mode in modes}
    for mode, aggregate in aggregates.items():
        selected = [run for run in runs if run["mode"] == mode]
        aggregate["mean_actual_tool_calls"] = _mean(
            [run["tool_call_count"] for run in selected])
        aggregate["mcp_adoption_rate"] = _mean([
            float(any(call.startswith(MCP_TOOL_PREFIX)
                      for call in run["tool_calls"]))
            for run in selected])
    return aggregates


def _run_once(item: PreparedCase, mode: str, repetition: int,
              command: list[str], output_dir: str, timeout_seconds: int,
              executor: AgentExecutor) -> dict:
    prompt = _prompt(item, mode)
    profile = "native" if mode == "native_agent" else "full_project"
    db_path = (Path(output_dir).resolve() / "indexes" / item.case.case_id /
               f"{mode}-{repetition}.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    environment = {
        "CRAI_EVAL_MODE": mode, "CRAI_EVAL_CASE": item.case.case_id,
        "CRAI_EVAL_TOOL_PROFILE": profile,
        "CRAI_EVAL_DB_PATH": str(db_path),
    }
    run = executor(command, prompt, item.repo_path, environment, timeout_seconds)
    payload, parse_error = _parse_agent_output(run.stdout)
    score = _score(payload.get("findings", []), item.case.gold_findings)
    result = {
        "case_id": item.case.case_id, "repo_name": item.case.repo_name,
        "mode": mode, "repetition": repetition,
        "source_commit": item.case.source_commit,
        "success": run.returncode == 0 and parse_error is None,
        "returncode": run.returncode, "elapsed_ms": round(run.elapsed_ms, 3),
        "parse_error": parse_error, **score,
        "files_read": _string_values(payload.get("files_read")),
        "tool_calls": _string_values(payload.get("tool_calls")),
        "tool_call_count": payload.get("tool_call_count", 0)
        if isinstance(payload.get("tool_call_count"), int) else 0,
        "context_files": [], "usage": _usage(payload, prompt, run.stdout),
    }
    _write_transcript(output_dir, item, mode, repetition, prompt, run, payload,
                      result)
    return result


def _prompt(item: PreparedCase, mode: str) -> str:
    contract = {
        "findings": [{"file": "path", "line": 1, "title": "...",
                      "description": "..."}],
        "files_read": [], "tool_calls": [],
    }
    project_note = ("You have native Read/Glob/Grep tools and the installed "
                    "code-review-ai MCP tools (the index is already current; "
                    "do not call rebuild_index). Call get_change_summary for "
                    "the changed files, then follow the decision table above: "
                    "only changes it flags as needing context get graph "
                    "queries, at the depth it prescribes (get_impact for "
                    "cross-service / deleted / cross-module interface changes, "
                    "query_graph for other context-needing changes, read call "
                    "sites for private changes). Self-contained changes need "
                    "no graph queries. Use search_symbol and get_symbol_detail "
                    "for individual symbols, and pass a small max_neighbors "
                    "when you do call query_graph. "
                    if mode == "full_project_agent" else
                    "You have native Read/Glob/Grep tools. ")
    return (
        f"You are running a controlled review of a real patch in {item.case.repo_name}.\n"
        f"{project_note}Inspect the repository as needed, but do not modify it.\n"
        "Report only concrete regressions introduced by the supplied diff. "
        f"{_REVIEW_PREFIX}"
        "Return exactly one JSON object matching this shape:\n"
        f"{json.dumps(contract)}\n\nTASK\n{item.case.prompt}\n\n"
        f"DIFF\n{item.diff}"
    )


def _write_transcript(output_dir: str, item: PreparedCase, mode: str,
                      repetition: int, prompt: str, run: AgentRun,
                      payload: dict, result: dict) -> None:
    path = Path(output_dir) / "transcripts" / item.case.case_id / mode
    path.mkdir(parents=True, exist_ok=True)
    record = {"repo_path": item.repo_path, "prompt": prompt,
              "stdout": run.stdout, "stderr": run.stderr,
              "parsed_output": payload, "result": result}
    (path / f"run-{repetition}.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")


def _validate(cases: list[FullAgentCase], command: list[str],
              modes: tuple[str, ...], repetitions: int,
              timeout_seconds: int, workers: int) -> None:
    if not cases or not command:
        raise ValueError("cases and agent command are required")
    if not modes or any(mode not in FULL_EVAL_MODES for mode in modes):
        raise ValueError(f"modes must be selected from {', '.join(FULL_EVAL_MODES)}")
    if repetitions < 1 or timeout_seconds < 1 or workers < 1:
        raise ValueError("repetitions, timeout, and workers must be at least 1")


def _run_git(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    completed = subprocess.run(["git", *args], input=stdin, capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
    if completed.returncode:
        raise ValueError(f"git {' '.join(args[:4])} failed: {completed.stderr.strip()}")
    return completed


def _restore_parent_version(worktree: Path, commit: str, path: str) -> None:
    """Make one fixed production path match its parent, leaving an unstaged diff."""
    parent_object = subprocess.run(
        ["git", "-C", str(worktree), "cat-file", "-e", f"{commit}^:{path}"],
        capture_output=True,
    )
    if parent_object.returncode == 0:
        _run_git(["-C", str(worktree), "checkout", f"{commit}^", "--", path])
        _run_git(["-C", str(worktree), "reset", "HEAD", "--", path])
        return
    current_object = subprocess.run(
        ["git", "-C", str(worktree), "cat-file", "-e", f"{commit}:{path}"],
        capture_output=True,
    )
    if current_object.returncode != 0:
        raise ValueError(f"mutation path is absent from fix and parent: {path}")
    _run_git(["-C", str(worktree), "rm", "--force", "--", path])
    _run_git(["-C", str(worktree), "reset", "HEAD", "--", path])


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0
