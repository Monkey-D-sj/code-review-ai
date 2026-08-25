"""End-to-end evaluation with real repositories and real agent tool use."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from code_review_ai.agent_eval import (
    AgentExecutor, AgentRun, DEFAULT_DIFFICULTY, DIFFICULTIES, GoldFinding,
    _execute_agent, _mode_metrics,
    _parse_agent_output, _parse_gold, _score, _string_values, _usage,
    SHARED_REVIEW_POLICY,
)
from code_review_ai.agent_adapter import MCP_TOOL_NAMES
from code_review_ai.changes import (
    detect_changed_symbols,
    detect_changed_symbols_from_patch,
)
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.impact import get_impact
from code_review_ai.indexer import rebuild


FULL_EVAL_MODES = ("native_agent", "full_project_agent",
                   "full_project_querygraph", "full_project_summary",
                   "full_project_search", "full_project_core")
DEFAULT_FULL_EVAL_MODES = ("native_agent", "full_project_core")

# MCP tool subset each online-ablation mode exposes, fed to the agent via
# CRAI_EVAL_MCP_TOOLS and to the server via CRAI_MCP_ONLY_TOOLS (server-side
# registration filter). None = the full online tool set.
_CORE_EXCLUDED_MCP_TOOLS = {
    "rebuild_index", "get_communities", "get_community", "call_external_service",
    "find_dead_code", "query_graph",
}
_CORE_MCP_TOOLS = tuple(
    name for name in MCP_TOOL_NAMES if name not in _CORE_EXCLUDED_MCP_TOOLS)

_MODE_MCP_TOOLS: dict[str, tuple[str, ...] | None] = {
    "full_project_agent": None,
    "full_project_querygraph": ("query_graph",),
    "full_project_summary": ("get_change_summary",),
    "full_project_search": ("search_symbol",),
    "full_project_core": _CORE_MCP_TOOLS,
}

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_READ_ONLY_REVIEW_POLICY = """本评估强制以只读方式执行。你只能使用 Read、Glob、Grep、允许列表中的只读 Bash 命令（例如
rg/grep），以及明确开放的只读 MCP 工具检查仓库内容。完整差异已提供在任务中，Bash git diff 也不可用。允许列表之外的 Bash 命令会被拒绝。
你不能运行脚本、测试、包管理器、解释器或网络命令，也不能安装依赖或修改文件。不要尝试执行上述任何操作。
禁止使用 git log、git show 或任何 git diff 命令读取提交历史，因为评测仓库的历史提交包含正确修复答案。"""


# The task block is deliberately case-independent: it states the deliverable
# (what broke, which callers / entry points / tests are affected) without naming
# a single symbol.  Per-case prose lives in ``FullAgentCase.hint`` and reaches
# the model only under ``hinted=True``, because naming the affected callers
# removes exactly the work the graph tools exist to do: the same text is
# symmetric input to both arms but asymmetric benefit — it hands the native arm
# the one hop it would otherwise have to traverse for.
_BLIND_TASK = """评审下方差异引入的回归。自行判断该变更破坏了什么，并沿调用链确定其影响范围：
哪些调用方会因此行为异常、哪些对外入口（HTTP 接口 / CLI / 定时任务）受影响、以及应当运行哪些测试。
差异之外的受影响文件必须显式报告。"""

# Gold sets carry one finding per case, so precision is 1/N of whatever the
# agent volunteers.  Uncapped, a blind task turns f1 into a verbosity measure.
_MAX_FINDINGS = 3


@dataclass(frozen=True)
class FullAgentCase:
    case_id: str
    repo_name: str
    repo_url: str
    source_commit: str
    mutation_paths: tuple[str, ...]
    hint: str
    gold_findings: tuple[GoldFinding, ...]
    complexity_tags: tuple[str, ...] = ()
    difficulty: str = DEFAULT_DIFFICULTY
    patch: str | None = None
    source_dir: str | None = None


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
    # Case-specific prose. Optional, and not injected unless the run is hinted.
    # Legacy manifests spell this key ``prompt``.
    hint = record.get("hint", record.get("prompt", ""))
    golds = record.get("gold_findings")
    complexity_tags = record.get("complexity_tags", [])
    difficulty = record.get("difficulty", DEFAULT_DIFFICULTY)
    patch = record.get("patch")
    source_dir = record.get("source_dir")
    names = (case_id, repo_name)
    if not all(isinstance(value, str) and _SAFE_NAME.match(value)
               for value in names):
        raise ValueError(f"case {position} has an invalid id or repo_name")
    if not isinstance(repo_url, str) or (
            repo_url and not repo_url.startswith("https://")):
        raise ValueError(
            f"case {case_id} requires an HTTPS repo_url "
            "(or empty to use a local repo)")
    if not isinstance(hint, str):
        raise ValueError(f"case {case_id} has a non-string hint")
    if patch is None:
        # git reverse-mutation mode: needs a real fix commit + mutation paths
        if not isinstance(commit, str) or not commit:
            raise ValueError(f"case {case_id} requires source_commit")
        if not isinstance(paths, list) or not paths or not all(
                isinstance(path, str) and path and not Path(path).is_absolute()
                and ".." not in Path(path).parts for path in paths):
            raise ValueError(f"case {case_id} has invalid mutation_paths")
    else:
        # inline-patch mode: the fixed→buggy diff is provided directly, no git
        # history needed. source_commit / mutation_paths are irrelevant.
        if not isinstance(patch, str) or not patch.strip():
            raise ValueError(f"case {case_id} has an empty patch")
        if not isinstance(source_dir, str) or not source_dir:
            raise ValueError(
                f"case {case_id} requires source_dir for patch mode")
        if not isinstance(commit, str):
            commit = ""
        if not isinstance(paths, list):
            paths = []
    if not isinstance(golds, list) or not golds:
        raise ValueError(f"case {case_id} requires gold_findings")
    if not isinstance(complexity_tags, list) or not all(
            isinstance(tag, str) and tag for tag in complexity_tags):
        raise ValueError(f"case {case_id} has invalid complexity_tags")
    if difficulty not in (*DIFFICULTIES, DEFAULT_DIFFICULTY):
        raise ValueError(
            f"case {case_id} has invalid difficulty; expected one of "
            f"{', '.join(DIFFICULTIES)}")
    findings = tuple(_parse_gold(gold, case_id) for gold in golds)
    return FullAgentCase(case_id, repo_name, repo_url, commit, tuple(paths),
                         hint, findings, tuple(complexity_tags), difficulty,
                         patch, source_dir)


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
                             work_dir: str,
                             local_repo: str | None = None) -> list[PreparedCase]:
    """Clone/cache sources and reverse real fixes in isolated git worktrees.

    With ``local_repo`` set, all cases must have an empty ``repo_url`` and
    share one committed seed repo (e.g. ``benchmarks/fast-repo``) whose
    ``build_repo.py`` materializes an isolated git history under ``repos_dir``;
    no remote clone is performed.
    """
    repo_root = Path(repos_dir).resolve()
    work_root = Path(work_dir).resolve() / "worktrees"
    repo_root.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    local_source = (_ensure_local_repo(cases, local_repo, repo_root)
                    if local_repo else None)
    prepared = []
    for case in cases:
        if case.patch is not None:
            scratch, diff = _prepare_patch_repo(case, work_root)
            prepared.append(PreparedCase(case, str(scratch), diff))
            continue
        if local_source is not None:
            source = local_source
        else:
            source = repo_root / case.repo_name
            if not source.exists():
                _run_git(["clone", "--filter=blob:none", case.repo_url,
                          str(source)])
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
                         "--unified=3", "--", *case.mutation_paths]).stdout
        if not diff.strip():
            raise ValueError(f"case {case.case_id} produced an empty mutation")
        prepared.append(PreparedCase(case, str(worktree), diff))
    return prepared


def _case_config(item: PreparedCase, db_path: str):
    config = load_config(item.repo_path)
    config.repo_path = item.repo_path
    config.db_path = db_path
    config.diff_base = "HEAD"
    config.community_detection = False
    # The eval prompt already embeds the full mutation diff; let
    # get_change_summary return metadata-only instead of re-inlining the same
    # per-function hunks (a duplicate fresh-token cost in the MCP result).
    config.summary_source = "none"
    return config


def _prepare_case_index(item: PreparedCase, work_dir: str,
                        label: str) -> dict:
    """Build the graph before the timed agent run and report setup separately."""
    snapshot_name = Path(item.repo_path).name
    db_path = (Path(work_dir).resolve() / "indexes" / item.case.case_id /
               f"{snapshot_name}-{label}.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    config = _case_config(item, str(db_path))
    conn = connect(str(db_path))
    init_schema(conn)
    started = time.perf_counter()
    try:
        stats = rebuild(config, conn)
    finally:
        conn.close()
    return {
        "case_id": item.case.case_id,
        "db_path": str(db_path),
        "nodes": stats.node_count,
        "edges": stats.edge_count,
        "flows": stats.flow_count,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "timed_with_agent": False,
    }


def preflight_full_agent_eval(cases: list[FullAgentCase], repos_dir: str,
                              work_dir: str,
                              local_repo: str | None = None) -> dict:
    prepared = prepare_full_agent_cases(cases, repos_dir, work_dir,
                                        local_repo=local_repo)
    results = []
    for item in prepared:
        setup = _prepare_case_index(item, work_dir, "preflight")
        config = _case_config(item, setup["db_path"])
        conn = connect(setup["db_path"])
        if item.case.patch is not None:
            # Patch-mode scratch repo has no git history to diff against;
            # attribute the inline diff's hunks to symbols instead.
            symbols = detect_changed_symbols_from_patch(config, item.diff)
        else:
            symbols = detect_changed_symbols(config)
        impacts = get_impact(conn, symbols, tests="include")
        results.append({
            "case_id": item.case.case_id, "repo_name": item.case.repo_name,
            "source_commit": item.case.source_commit,
            "complexity_tags": list(item.case.complexity_tags),
            "difficulty": item.case.difficulty,
            "repo_path": item.repo_path, "diff_characters": len(item.diff),
            "changed_symbols": symbols,
            "found_symbols": [impact["symbol"] for impact in impacts
                              if impact["found"]],
            "index": {key: setup[key] for key in (
                "nodes", "edges", "flows", "elapsed_ms")},
        })
        conn.close()
    total_symbols = sum(len(result["changed_symbols"]) for result in results)
    found = sum(len(result["found_symbols"]) for result in results)
    return {"schema_version": 2, "dry_run": True,
            "evaluation": "full_project_online_tool_use", "cases": results,
            "aggregate": {"case_count": len(results),
                          "difficulty_counts": _difficulty_counts(cases),
                          "changed_symbols": total_symbols,
                          "symbol_found_rate": round(found / total_symbols, 4)
                          if total_symbols else 0.0}}


def run_full_agent_eval(cases: list[FullAgentCase], repos_dir: str,
                        work_dir: str, command: list[str],
                        modes: tuple[str, ...] = DEFAULT_FULL_EVAL_MODES,
                        repetitions: int = 1, timeout_seconds: int = 600,
                        workers: int = 1,
                        executor: AgentExecutor | None = None,
                        local_repo: str | None = None,
                        hinted: bool = False) -> dict:
    _validate(cases, command, modes, repetitions, timeout_seconds, workers)
    prepared = prepare_full_agent_cases(cases, repos_dir, work_dir,
                                        local_repo=local_repo)
    index_setups = {
        item.case.case_id: _prepare_case_index(item, work_dir, "online")
        for item in prepared
    }
    execute_agent = executor or _execute_agent
    jobs = [(item, mode, repetition) for item in prepared for mode in modes
            for repetition in range(1, repetitions + 1)]

    def execute(job: tuple[PreparedCase, str, int]) -> dict:
        item, mode, repetition = job
        return _run_once(item, mode, repetition, command, work_dir,
                         timeout_seconds, execute_agent,
                         index_setups[item.case.case_id]["db_path"],
                         hinted=hinted)

    if workers == 1:
        runs = [execute(job) for job in jobs]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            runs = list(pool.map(execute, jobs))
    aggregates = _full_aggregates(runs, modes)
    return {"schema_version": 2,
            "evaluation": "full_project_online_tool_use",
            "baseline_mode": "native_agent",
            "modes": list(modes), "repetitions": repetitions,
            "hint_mode": "hinted" if hinted else "blind",
            "guidance_mode": "stripped" if _guidance_stripped() else "full",
            "workers": workers, "aggregate": aggregates, "runs": runs,
            "difficulty_counts": _difficulty_counts(cases),
            "index_setup": list(index_setups.values()),
            "cases": [{"id": item.case.case_id,
                       "repo_name": item.case.repo_name,
                       "repo_url": item.case.repo_url,
                       "source_commit": item.case.source_commit,
                       "difficulty": item.case.difficulty,
                       "complexity_tags": list(item.case.complexity_tags),
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
        run["difficulty"] = by_id[case_id].difficulty
        run["complexity_tags"] = list(by_id[case_id].complexity_tags)
        calls = [call for call in run.get("tool_calls", [])
                 if call in {"Read", "Glob", "Grep"}
                 or (isinstance(call, str) and call.startswith("mcp__code-review-ai__"))]
        removed = len(run.get("tool_calls", [])) - len(calls)
        run["tool_calls"] = calls
        run["tool_call_count"] = max(
            0, int(run.get("tool_call_count", 0)) - removed)
    modes = tuple(report.get("modes") or dict.fromkeys(
        run["mode"] for run in runs))
    report["aggregate"] = _full_aggregates(runs, modes)
    report["difficulty_counts"] = _difficulty_counts(cases)
    if report.get("index_setup") and all(
            run.get("index_prebuilt") is True for run in runs):
        report["schema_version"] = 2
        report["evaluation"] = "full_project_online_tool_use"
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
        failures = Counter()
        for run in selected:
            if not run["success"]:
                failures[run.get("failure_reason") or "unclassified"] += 1
        aggregate["failure_reasons"] = dict(failures.most_common())
        aggregate["mean_actual_tool_calls"] = _mean(
            [run["tool_call_count"] for run in selected])
        aggregate["mcp_adoption_rate"] = _mean([
            float(any(call.startswith("mcp__") for call in run["tool_calls"]))
            for run in selected])
        aggregate["mcp_tool_adoption_rate"] = {
            name: _mean([
                float(f"mcp__code-review-ai__{name}" in run["tool_calls"])
                for run in selected
            ])
            for name in (
                "rebuild_index", "get_change_summary", "get_change_context",
                "query_graph",
                "get_test_impact", "search_symbol",
                "get_symbol_detail",
            )
        }
    return aggregates


def _difficulty_counts(cases: list[FullAgentCase]) -> dict[str, int]:
    counts = Counter(case.difficulty for case in cases)
    ordered = (*DIFFICULTIES, DEFAULT_DIFFICULTY)
    return {difficulty: counts[difficulty]
            for difficulty in ordered if counts[difficulty]}


def _run_once(item: PreparedCase, mode: str, repetition: int,
              command: list[str], output_dir: str, timeout_seconds: int,
              executor: AgentExecutor, db_path: str,
              hinted: bool = False) -> dict:
    prompt = _prompt(item, mode, hinted)
    profile = "native" if mode == "native_agent" else "full_project"
    environment = {
        "CRAI_EVAL_MODE": mode, "CRAI_EVAL_CASE": item.case.case_id,
        "CRAI_EVAL_TOOL_PROFILE": profile,
        "CRAI_EVAL_DB_PATH": str(db_path),
    }
    tools = _MODE_MCP_TOOLS.get(mode)
    if tools:
        # Ablation: the model sees only this MCP subset (both the agent-side
        # allowedTools filter and the server-side registration filter).
        environment["CRAI_EVAL_MCP_TOOLS"] = ",".join(tools)
    run = executor(command, prompt, item.repo_path, environment, timeout_seconds)
    payload, parse_error = _parse_agent_output(run.stdout)
    score = _score(payload.get("findings", []), item.case.gold_findings)
    result = {
        "case_id": item.case.case_id, "repo_name": item.case.repo_name,
        "mode": mode, "repetition": repetition,
        "source_commit": item.case.source_commit,
        "complexity_tags": list(item.case.complexity_tags),
        "difficulty": item.case.difficulty,
        "index_prebuilt": True,
        "hint_mode": "hinted" if hinted else "blind",
        "guidance_mode": "stripped" if _guidance_stripped() else "full",
        "success": run.returncode == 0 and parse_error is None,
        "returncode": run.returncode, "elapsed_ms": round(run.elapsed_ms, 3),
        "parse_error": parse_error,
        "failure_reason": payload.get("failure_reason")
        if isinstance(payload, dict) else None,
        **score,
        "files_read": _string_values(payload.get("files_read")),
        "tool_calls": _string_values(payload.get("tool_calls")),
        "tool_call_count": payload.get("tool_call_count", 0)
        if isinstance(payload.get("tool_call_count"), int) else 0,
        "tool_trace": payload.get("tool_trace", [])
        if isinstance(payload.get("tool_trace"), list) else [],
        "context_files": [], "usage": _usage(payload, prompt, run.stdout),
    }
    _write_transcript(output_dir, item, mode, repetition, prompt, run, payload,
                      result)
    return result


def _hint_block(hint: str, hinted: bool) -> str:
    """Case-specific prose, appended only for the ``hinted`` ablation arm."""
    if not (hinted and hint):
        return ""
    return """

补充说明
""" + hint


def _guidance_stripped() -> bool:
    """Ablation arm: strip the prompt-side tool-usage / review-methodology
    guidance so the model must derive tool selection and call-graph traversal
    from the MCP tool descriptions alone (``CRAI_EVAL_NO_GUIDANCE=1``)."""
    return os.environ.get("CRAI_EVAL_NO_GUIDANCE", "").strip().lower() in {
        "1", "true", "yes"}


def _prompt(item: PreparedCase, mode: str, hinted: bool = False) -> str:
    contract = {
        "findings": [{"file": "path", "line": 1, "title": "...",
                      "description": "..."}],
        "files_read": [], "tool_calls": [],
    }
    if mode == "full_project_agent":
        tool_note = """你可以使用原生只读检查工具以及已安装的 code-review-ai MCP 工具。图索引已同步；
不要调用 rebuild_index。使用 get_change_summary 获取结构化变更详情，使用 query_graph 获取上游或下游邻居。
当更广泛的影响范围不确定时，使用 query_graph（根据需要反复指定 direction=in 或 out）遍历调用图。
风险是排序信号，不是硬性门槛。"""
    elif mode == "full_project_querygraph":
        tool_note = """你可以使用原生只读检查工具以及 query_graph MCP 工具；该工具返回某个符号已解析的调用图邻居
（in = 上游调用方，out = 下游被调用方）。图索引已同步；不要调用 rebuild_index。整个评审中最多调用两次 query_graph，
每次使用 max_neighbors=5。不要查询每个变更符号。只选择最可能暴露运行时使用方链路和公共/API 契约链路的结构性契约节点。
默认使用 direction=in；仅当补丁改变参数、返回值或下游调用时使用 direction=out。在这两条链路均已表示后停止图探索。
不要 grep，也不要重新读取图响应中已经存在的关系。"""
    elif mode == "full_project_summary":
        tool_note = """你可以使用原生只读检查工具以及 get_change_summary MCP 工具；该工具会报告哪些符号发生了变更以及
变更位置（文件/行号/签名）；差异内容已内嵌在下方的任务中。图索引已同步；不要调用 rebuild_index。
先调用 get_change_summary 了解变更符号，然后使用原生工具定位并读取这些符号的调用方和被调用方。"""
    elif mode == "full_project_search":
        tool_note = """你可以使用原生只读检查工具以及 search_symbol MCP 工具；该工具可以按名称或 glob 查找符号的限定名称
（qname）。图索引已同步；不要调用 rebuild_index。从下方的差异中识别变更符号，使用 search_symbol 解析它们的 qname，
然后使用原生工具定位这些符号的调用方和被调用方。"""
    elif mode == "full_project_core":
        tool_note = """你可以使用原生只读检查工具以及这些 code-review-ai MCP 工具：get_impact、get_test_impact、
get_change_summary、get_change_context、search_symbol 和 get_symbol_detail。未开放 rebuild_index、query_graph、
get_communities、get_community、call_external_service、find_dead_code。评审主通道是 get_impact：
先用 get_change_summary 获取结构化变更符号，然后对每个关键变更符号调用一次 get_impact，一次性获取其传递调用链
（upstream/downstream，整图 BFS 精确闭包）与受影响业务入口（affected_entries）——这是本模式区别于 grep 的核心价值；
其 uncertainty 已列出解析缺口、coverage 已给出解析覆盖率。不要在 get_impact 已覆盖的符号上重复调用 get_change_context。
仅当需要判断某个具体调用点的契约变更（参数、返回值、异常）时，才对那个符号调用 get_change_context
补齐调用点代码片段（call_site.code）。get_test_impact 仅用于测试影响；search_symbol、get_symbol_detail 仅用于解析
不确定的 qname。图索引已同步。不要 grep，也不要重新读取 MCP 响应中已经存在的关系；仅使用原生工具验证缺失证据
或具体候选问题。"""
    else:
        tool_note = """你可以使用原生只读检查工具。使用这些工具获取评审策略所需的仓库证据；对于涉及签名、返回类型、异常或
跨模块调用的任何变更，使用 Grep 或 rg 搜索整个代码树，定位并读取所有调用方和被调用方。"""
    if _guidance_stripped():
        # Ablation arm: drop the prompt-side tool-usage / review-methodology
        # guidance so the model must derive tool selection and traversal from
        # the MCP tool descriptions alone.
        tool_note = ""
        policy = ""
    else:
        policy = SHARED_REVIEW_POLICY
    return f"""你正在对 {item.case.repo_name} 中的真实补丁执行受控评审。
{policy}
{_READ_ONLY_REVIEW_POLICY}
{tool_note}
Read 文件必须基于行号精读：优先利用图工具返回的 line / call_site.line（或 rg 输出的行号），用 offset/limit 只读取目标段落；禁止无 offset/limit 地读取整个文件。唯一的例外：仅当目标文件是本次 diff 中的变更文件本身（需要查看变更函数自身的完整上下文）时才允许全文读取；调用方、被调用方、依赖文件一律精读。
根据需要检查仓库，但不要修改仓库。
只报告由所提供差异引入的具体回归问题。将同一缺陷的多个表现合并为一个发现；只有独立根因才单独报告。
按独立修复单元组织发现：同一错误代码位置、同一必要修复产生的多个表现应合并；如果修复一个生产代码位置后另一个回归仍然存在，则必须分别报告。不要按请求类型、调用方或测试用例拆分，也不要用一个宽泛总括项吞并多个可独立修复的缺陷。输出前删除同一修复的重复表现，并确认每条发现都有独立的生产代码修复位置。
最多报告 {_MAX_FINDINGS} 条发现，按严重度降序排列；宁缺毋滥，不要为凑数报告推测性问题。
必须严格返回一个符合以下结构的 JSON 对象：
{json.dumps(contract)}

任务
{_BLIND_TASK}{_hint_block(item.case.hint, hinted)}

差异
{item.diff}"""


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


def _run_git(args: list[str], stdin: str | bytes | None = None) -> subprocess.CompletedProcess:
    if isinstance(stdin, bytes):
        # Bytes stdin: keep newlines verbatim. text=True would translate \n to \r\n
        # on Windows, breaking git apply against LF-checked-out working trees.
        completed = subprocess.run(["git", *args], input=stdin, capture_output=True)
        completed.stdout = completed.stdout.decode("utf-8", "replace")
        completed.stderr = completed.stderr.decode("utf-8", "replace")
    else:
        completed = subprocess.run(["git", *args], input=stdin, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace")
    if completed.returncode:
        raise ValueError(f"git {' '.join(args[:4])} failed: {completed.stderr.strip()}")
    return completed


def _run_python(args: list[str], cwd: Path) -> None:
    completed = subprocess.run([sys.executable, *args], cwd=str(cwd),
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise ValueError(f"python {' '.join(args[:2])} failed: {detail}")


def _ensure_local_repo(cases: list[FullAgentCase], local_repo: str,
                       repo_root: Path) -> Path:
    """Validate local-repo mode and materialize the synthetic repo in the cache.

    ``local_repo`` names the committed seed (a pure source tree, e.g.
    ``benchmarks/fast-repo``); the built git repo lives at
    ``<repos_dir>/<repo_name>`` so the seed is never mutated and the parent
    repo never sees a nested ``.git``. All cases must share one ``repo_name``
    and carry an empty ``repo_url``. ``build_repo.py`` is idempotent
    (marker-hash) and rebuilds automatically when the seed changes.
    """
    offenders = sorted(case.case_id for case in cases if case.repo_url)
    if offenders:
        raise ValueError(
            "local-repo cases must have an empty repo_url: "
            + ", ".join(offenders))
    names = sorted({case.repo_name for case in cases})
    if len(names) != 1:
        raise ValueError(
            "local-repo cases must share one repo_name: "
            + ", ".join(names))
    seed = Path(local_repo).resolve()
    if not seed.is_dir():
        raise ValueError(f"local repo seed does not exist: {seed}")
    builder = seed / "build_repo.py"
    if not builder.exists():
        raise ValueError(f"local repo seed has no build_repo.py: {seed}")
    target = repo_root / names[0]
    _run_python([str(builder), "--seed", str(seed),
                 "--target", str(target)], cwd=seed)
    return target


def _prepare_patch_repo(case: FullAgentCase, work_root: Path) -> tuple[Path, str]:
    """Materialize a patch-mode case: copy the pristine source dir, git-init it,
    and apply the inline fixed→buggy diff. No clone / worktree / commit history —
    git is used only as plumbing so ``rebuild``'s ``git ls-files`` file listing
    works. Returns (scratch_repo, diff_text)."""
    source = Path(case.source_dir).resolve()
    if not source.is_dir():
        raise ValueError(f"patch case source_dir is not a directory: {source}")
    scratch = work_root / f"{case.case_id}-{uuid.uuid4().hex[:8]}"
    shutil.copytree(source, scratch)
    _run_git(["-C", str(scratch), "init", "-q"])
    _run_git(["-C", str(scratch), "add", "-A"])
    # Anchor a local commit at the pristine tree so the MCP tools' git-diff
    # paths (get_change_summary / get_impact with no symbols) see
    # ``git diff HEAD`` as exactly this mutation — no upstream / history needed.
    _run_git(["-C", str(scratch), "-c", "user.name=code-review-ai-eval",
              "-c", "user.email=code-review-ai-eval@local",
              "commit", "-m", "pristine source"])
    _run_git(["-C", str(scratch), "apply"], stdin=case.patch.encode("utf-8"))
    return scratch, case.patch


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
