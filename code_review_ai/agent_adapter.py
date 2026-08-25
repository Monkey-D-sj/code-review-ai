"""Provider adapters that normalize agent CLIs to the agent-eval JSON contract."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["file", "line", "title", "description"],
                "additionalProperties": False,
            },
        },
        "files_read": {"type": "array", "items": {"type": "string"}},
        "tool_calls": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["findings", "files_read", "tool_calls"],
    "additionalProperties": False,
}


MCP_TOOL_NAMES = (
    "rebuild_index", "get_impact", "get_test_impact", "find_dead_code",
    "get_change_summary", "get_change_context", "query_graph", "search_symbol",
    "get_symbol_detail", "get_communities",
    "get_community", "call_external_service",
)
ONLINE_MCP_TOOL_NAMES = tuple(
    name for name in MCP_TOOL_NAMES
    if name not in {"rebuild_index", "call_external_service"})
READ_ONLY_NATIVE_TOOLS = ("Read", "Glob", "Grep", "Bash")
READ_ONLY_BASH_RULES = (
    "Bash(rg *)",
    "Bash(grep *)",
    "Bash(git status *)",
    "Bash(git rev-parse *)",
    "Bash(ls *)",
    "Bash(cat *)",
    "Bash(head *)",
    "Bash(tail *)",
    "Bash(wc *)",
    "Bash(stat *)",
    "Bash(du *)",
    "Bash(diff *)",
)
# Deny rules win over allow rules. In particular, ripgrep's --pre option can
# execute an arbitrary preprocessor and is therefore not a read-only search.
DENIED_BASH_RULES = (
    "Bash(*>*)",
    "Bash(*<*)",
    "Bash(rg *--pre *)",
    "Bash(rg *--pre=*)",
    "Bash(python *)",
    "Bash(python3 *)",
    "Bash(py *)",
    "Bash(pip *)",
    "Bash(pip3 *)",
    "Bash(uv *)",
    "Bash(npm *)",
    "Bash(npx *)",
    "Bash(curl *)",
    "Bash(wget *)",
    "Bash(rm *)",
    "Bash(mv *)",
    "Bash(cp *)",
    "Bash(mkdir *)",
    "Bash(touch *)",
    "Bash(tee *)",
    "Bash(truncate *)",
    "Bash(chmod *)",
    "Bash(chown *)",
    "Bash(git checkout *)",
    "Bash(git reset *)",
    "Bash(git clean *)",
    "Bash(git restore *)",
    "Bash(git switch *)",
    "Bash(git commit *)",
    "Bash(git merge *)",
    "Bash(git rebase *)",
    "Bash(git apply *)",
    "Bash(git am *)",
    # Eval worktrees reverse a known fix commit; history is the answer key.
    "Bash(git show)",
    "Bash(git show *)",
    "Bash(git log)",
    "Bash(git log *)",
    # Claude Bash permission patterns are prefix-oriented, so even an apparent
    # exact `Bash(git diff)` rule can admit revision arguments. The full diff
    # is already embedded in the eval prompt; deny the command entirely.
    "Bash(git diff)",
    "Bash(git diff *)",
)


def _online_mcp_tools() -> tuple[str, ...]:
    """The MCP tools exposed to the evaluated agent.

    ``CRAI_EVAL_MCP_TOOLS`` (comma-separated) narrows the full online set so a
    run can ablate a single tool (e.g. ``query_graph``); empty means all online
    tools. Filtering here (and only here) is what lets the model see just that
    tool: ``--allowedTools`` gates the agent's visible toolset."""
    subset = os.environ.get("CRAI_EVAL_MCP_TOOLS", "").strip()
    if not subset:
        return ONLINE_MCP_TOOL_NAMES
    wanted = {name.strip() for name in subset.split(",") if name.strip()}
    # An explicit eval-mode allowlist may intentionally opt into tools omitted
    # from the safe default. Unknown names are still dropped.
    return tuple(name for name in MCP_TOOL_NAMES if name in wanted)


def _resolve_claude_executable() -> str:
    """Resolve the claude launcher once so subprocess does not re-search PATH."""
    if os.name != "nt":
        return shutil.which("claude") or "claude"
    for name in ("claude.cmd", "claude.exe", "claude.bat", "claude"):
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return "claude.cmd"


def run_claude(prompt: str, model: str | None = None,
               max_budget_usd: float | None = None,
               tool_profile: str | None = None) -> tuple[int, dict, str]:
    """Run Claude Code and normalize its result plus observed tool events."""
    executable = _resolve_claude_executable()
    streaming = tool_profile in {"native", "full_project"}
    command = [executable, "-p", "--no-session-persistence",
               "--output-format", "stream-json" if streaming else "json",
               "--json-schema", json.dumps(FINDING_SCHEMA)]
    if streaming:
        command.extend(["--verbose", "--bare", "--disable-slash-commands",
                        "--permission-mode", "dontAsk",
                        "--tools", ",".join(READ_ONLY_NATIVE_TOOLS)])
        allowed = [*READ_ONLY_NATIVE_TOOLS[:-1], *READ_ONLY_BASH_RULES]
        if tool_profile == "full_project":
            command.extend(["--strict-mcp-config", "--mcp-config",
                            json.dumps(_mcp_config())])
            allowed.extend(f"mcp__code-review-ai__{name}"
                           for name in _online_mcp_tools())
        else:
            command.extend(["--strict-mcp-config", "--mcp-config",
                            json.dumps({"mcpServers": {}})])
        command.extend(["--allowedTools", ",".join(allowed)])
        command.extend(["--disallowedTools", ",".join(DENIED_BASH_RULES)])
    else:
        command.extend(["--safe-mode", "--tools", ""])
    if model:
        command.extend(["--model", model])
    if max_budget_usd is not None:
        command.extend(["--max-budget-usd", str(max_budget_usd)])
    completed = subprocess.run(command, input=prompt, capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        details = "\n".join(part for part in (completed.stderr, completed.stdout)
                            if part)
        return completed.returncode, _error_payload(completed.stdout), details
    try:
        if streaming:
            return 0, normalize_claude_stream(completed.stdout), completed.stderr
        outer = json.loads(completed.stdout)
        return 0, normalize_claude_result(outer), completed.stderr
    except (json.JSONDecodeError, ValueError) as exc:
        return 1, {}, f"unable to normalize Claude output: {exc}\n{completed.stderr}"


def _mcp_config() -> dict:
    db_path = os.environ.get("CRAI_EVAL_DB_PATH")
    if not db_path:
        db_path = str(Path.cwd() / ".code-review-ai" / "agent-eval.db")
    env = {
        "CRAI_REPO_PATH": str(Path.cwd()),
        "CRAI_DB_PATH": str(Path(db_path).resolve()),
        "CRAI_DIFF_BASE": "HEAD",
        "CRAI_COMMUNITY_DETECTION": "false",
        "CRAI_SKIP_STARTUP_SYNC": "true",
        "CRAI_DISABLE_WATCHER": "true",
        # Ablation: when the eval narrows MCP to a tool subset
        # (CRAI_EVAL_MCP_TOOLS), pass that subset to the server process so it
        # registers ONLY those tools. --allowedTools alone does not stop a
        # headless model from seeing/calling every tool the server exposes, so
        # the restriction must happen where the tools are defined.
        "CRAI_MCP_ONLY_TOOLS": os.environ.get("CRAI_EVAL_MCP_TOOLS", ""),
    }
    return {"mcpServers": {"code-review-ai": {
        "type": "stdio", "command": sys.executable,
        "args": ["-m", "code_review_ai.mcp_server"], "env": env,
    }}}


def _failure_reason(result_event: dict) -> str | None:
    subtype = result_event.get("subtype")
    if not isinstance(subtype, str) or subtype == "success":
        return None
    return subtype


def normalize_claude_stream(stdout: str) -> dict:
    """Normalize stream-json and derive telemetry from actual tool events."""
    events = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if isinstance(event, dict):
            events.append(event)
    result_event = next((event for event in reversed(events)
                         if event.get("type") == "result"), None)
    if result_event is None:
        raise ValueError("Claude stream has no result event")
    failure_reason = _failure_reason(result_event)
    try:
        payload = normalize_claude_result(result_event)
    except ValueError:
        # An error result (e.g. error_max_budget_usd) carries no findings but
        # has real usage and tool events; keep those instead of dropping the run.
        payload = {"findings": [], "files_read": [], "tool_calls": [],
                   "usage": _provider_usage(result_event)}
    if failure_reason:
        payload["failure_reason"] = failure_reason
    available_tools = set()
    for event in events:
        if event.get("type") == "system" and event.get("subtype") == "init":
            tools = event.get("tools")
            if isinstance(tools, list):
                available_tools.update(tool for tool in tools
                                       if isinstance(tool, str))
    calls: list[str] = []
    files: list[str] = []
    tool_trace = _build_tool_trace(events, available_tools)
    for event in events:
        for block in _tool_use_blocks(event):
            name = block.get("name")
            if not isinstance(name, str):
                continue
            if name == "StructuredOutput":
                continue
            if available_tools and name not in available_tools:
                continue
            calls.append(name)
            tool_input = block.get("input")
            if name == "Read" and isinstance(tool_input, dict):
                path = tool_input.get("file_path") or tool_input.get("path")
                if isinstance(path, str):
                    files.append(_relative_tool_path(path))
    payload["tool_calls"] = list(dict.fromkeys(calls))
    payload["tool_call_count"] = len(calls)
    payload["files_read"] = list(dict.fromkeys(files))
    payload["tool_trace"] = tool_trace
    return payload


def _build_tool_trace(events: list[dict], available_tools: set[str]) -> list[dict]:
    """Keep ordered tool inputs and response sizes without retaining responses.

    Claude's stream does not expose reliable per-tool wall time, but tool-use
    ids let us pair each call with its tool-result payload. This is enough to
    identify repeated graph calls and oversized responses after an eval run.
    """
    result_sizes: dict[str, int] = {}
    result_errors: dict[str, bool] = {}
    for event in events:
        for block in _typed_blocks(event, "tool_result"):
            tool_use_id = block.get("tool_use_id")
            if not isinstance(tool_use_id, str):
                continue
            result_sizes[tool_use_id] = _serialized_chars(block.get("content"))
            result_errors[tool_use_id] = block.get("is_error") is True

    trace: list[dict] = []
    for event in events:
        for block in _tool_use_blocks(event):
            name = block.get("name")
            if not isinstance(name, str) or name == "StructuredOutput":
                continue
            if available_tools and name not in available_tools:
                continue
            tool_use_id = block.get("id")
            record = {
                "sequence": len(trace) + 1,
                "tool": name,
                "input": _compact_tool_input(block.get("input")),
                "response_chars": result_sizes.get(tool_use_id, 0)
                if isinstance(tool_use_id, str) else 0,
            }
            if isinstance(tool_use_id, str) and result_errors.get(tool_use_id):
                record["is_error"] = True
            trace.append(record)
    return trace


def _typed_blocks(value: object, block_type: str):
    if isinstance(value, dict):
        if value.get("type") == block_type:
            yield value
        for child in value.values():
            yield from _typed_blocks(child, block_type)
    elif isinstance(value, list):
        for child in value:
            yield from _typed_blocks(child, block_type)


def _serialized_chars(value: object) -> int:
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return len(str(value))


def _compact_tool_input(value: object) -> object:
    """Bound trace telemetry while preserving qnames, paths and directions."""
    if isinstance(value, dict):
        result = {}
        for key, child in list(value.items())[:20]:
            if key in {"file_path", "path"} and isinstance(child, str):
                result[key] = _relative_tool_path(child)
            else:
                result[key] = _compact_tool_input(child)
        return result
    if isinstance(value, list):
        return [_compact_tool_input(child) for child in value[:20]]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "…"
    return value


def _tool_use_blocks(value: object):
    if isinstance(value, dict):
        if value.get("type") == "tool_use":
            yield value
        for child in value.values():
            yield from _tool_use_blocks(child)
    elif isinstance(value, list):
        for child in value:
            yield from _tool_use_blocks(child)


def _relative_tool_path(value: str) -> str:
    path = Path(value)
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def normalize_claude_result(outer: object) -> dict:
    """Convert Claude Code's JSON envelope into the agent-eval contract."""
    if not isinstance(outer, dict):
        raise ValueError("Claude output must be a JSON object")
    result = outer.get("structured_output")
    if not isinstance(result, dict):
        raw_result = outer.get("result")
        if not isinstance(raw_result, str):
            raise ValueError("Claude output has no structured_output or result")
        result = json.loads(_strip_fence(raw_result))
    if not isinstance(result.get("findings"), list):
        raise ValueError("Claude result has no findings array")
    # This adapter disables all repository tools. Do not trust model-authored
    # telemetry fields: a model can claim it used Read even when no tool was
    # available. Actual supplied files are measured by the eval runner.
    result["files_read"] = []
    result["tool_calls"] = []
    result["usage"] = _provider_usage(outer)
    return result


_EMPTY_CONTRACT = {"findings": [], "files_read": [], "tool_calls": [],
                   "usage": {}}


def _error_payload(stdout: str) -> dict:
    """Build a contract payload for a non-zero exit, preserving stream data.

    Streaming mode writes an NDJSON stream (with a terminal ``result`` event)
    to stdout; a budget/tool failure still carries real usage and tool events
    that must survive instead of being dropped as a single-line JSON parse
    error.
    """
    if not stdout:
        return dict(_EMPTY_CONTRACT)
    try:
        return normalize_claude_stream(stdout)
    except ValueError:
        pass
    try:
        outer = json.loads(stdout)
        if isinstance(outer, dict):
            return {"findings": [], "files_read": [], "tool_calls": [],
                    "usage": _provider_usage(outer)}
    except json.JSONDecodeError:
        pass
    return dict(_EMPTY_CONTRACT)


def _provider_usage(outer: dict) -> dict:
    usage = outer.get("usage") if isinstance(outer.get("usage"), dict) else {}
    model_usage = outer.get("modelUsage")
    model_record = next(iter(model_usage.values()), {}) \
        if isinstance(model_usage, dict) else {}
    input_tokens = _usage_value(usage, "input_tokens")
    output_tokens = _usage_value(usage, "output_tokens")
    if not input_tokens:
        input_tokens = _usage_value(model_record, "inputTokens")
    if not output_tokens:
        output_tokens = _usage_value(model_record, "outputTokens")
    return {
        "input_tokens": input_tokens,
        "cache_read_input_tokens": _usage_value(
            usage, "cache_read_input_tokens") or _usage_value(
                model_record, "cacheReadInputTokens"),
        "cache_creation_input_tokens": _usage_value(
            usage, "cache_creation_input_tokens") or _usage_value(
                model_record, "cacheCreationInputTokens"),
        "output_tokens": output_tokens,
        "total_cost_usd": outer.get("total_cost_usd"),
        "model": model_record.get("canonicalModel"),
    }


def _usage_value(usage: dict, key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) else 0


def _strip_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        return "\n".join(lines[1:-1])
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-review-ai-agent-adapter")
    subparsers = parser.add_subparsers(dest="provider", required=True)
    claude = subparsers.add_parser("claude")
    claude.add_argument("--model")
    claude.add_argument("--max-budget-usd", type=float)
    claude.add_argument("--tool-profile",
                        choices=["none", "native", "full_project"],
                        default=os.environ.get("CRAI_EVAL_TOOL_PROFILE", "none"))
    args = parser.parse_args(argv)
    prompt = sys.stdin.read()
    profile = None if args.tool_profile == "none" else args.tool_profile
    returncode, payload, error = run_claude(
        prompt, model=args.model, max_budget_usd=args.max_budget_usd,
        tool_profile=profile)
    if returncode == 0:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload if isinstance(payload, dict) and payload
                         else dict(_EMPTY_CONTRACT), ensure_ascii=False))
        print(error, file=sys.stderr)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
