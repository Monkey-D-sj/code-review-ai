"""Agent-execution and scoring primitives shared by the eval harnesses.

Holds exactly the pieces both harnesses need: run an agent command, parse its
stdout, score the findings against gold, and roll usage up into per-mode
metrics. Nothing here knows about a specific harness's case format -- the
caller supplies the case model and the gold.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from code_review_ai.eval_gold import GoldFinding, score_root_causes

DIFFICULTIES = ("trivial", "medium", "hard")
DEFAULT_DIFFICULTY = "unclassified"
SHARED_REVIEW_POLICY = """无论有哪些上下文工具可用，都应遵循此评审策略。对于每个发生变更的符号，
先检查差异及其局部代码，然后判断该变更是否自包含。只有在不改变公共签名、
返回类型、异常行为、外部可观察语义或跨模块调用的情况下，才将注释、格式调整、
仅重命名以及函数局部实现变更视为自包含。对于每个非自包含变更，先检查上游调用方。
当参数、调用或所使用的返回值发生变化时，还要检查下游被调用方。适用时，还应检查
相关测试、配置、路由、依赖注入和公共 API 边界。利用可用上下文仅收集完成此流程
所需的证据，不要重新读取已经掌握的证据。"""


@dataclass(frozen=True)
class AgentRun:
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: float


AgentExecutor = Callable[[list[str], str, str, dict[str, str], int], AgentRun]


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
    return score_root_causes(predictions, golds)


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
        "mean_unique_files_touched": _mean(
            results, lambda result: len(result.get("unique_files_touched", []))),
        "mean_read_calls": _mean(results, lambda result: result.get("read_calls", 0)),
        "mean_search_calls": _mean(
            results, lambda result: result.get("search_calls", 0)),
        "mean_bash_calls": _mean(results, lambda result: result.get("bash_calls", 0)),
        "unknown_file_access_rate": _mean(
            results, lambda result: float(result.get("unknown_file_access", False))),
        "mean_native_response_chars": _mean(
            results, lambda result: result.get("native_response_chars", 0)),
        "mean_mcp_response_chars": _mean(
            results, lambda result: result.get("mcp_response_chars", 0)),
        "mean_total_tool_calls": _mean(
            results, lambda result: result.get(
                "total_tool_calls", result.get("tool_call_count", 0))),
        "mean_total_tokens": _mean(
            results, lambda result: result.get("usage", {}).get("input_tokens", 0)
            + result.get("usage", {}).get("output_tokens", 0)),
        "mean_context_files": _mean(results, lambda result: len(result["context_files"])),
        "mean_tool_calls": _mean(results, lambda result: len(result["tool_calls"])),
    }


def _mean(results: list[dict], getter: Callable[[dict], float]) -> float:
    return round(sum(getter(result) for result in results) / len(results), 4) if results else 0.0


def _string_values(value: object) -> list[str]:
    return list(dict.fromkeys(item for item in value
                              if isinstance(item, str))) if isinstance(value, list) else []
