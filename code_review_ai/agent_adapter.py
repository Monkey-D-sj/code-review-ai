"""Provider adapters that normalize agent CLIs to the agent-eval JSON contract."""

from __future__ import annotations

import argparse
import json
import os
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
    "get_change_summary", "query_graph", "search_symbol",
    "get_symbol_detail", "list_entry_points", "get_communities",
    "get_community",
)
ONLINE_MCP_TOOL_NAMES = tuple(
    name for name in MCP_TOOL_NAMES if name != "rebuild_index")


def run_claude(prompt: str, model: str | None = None,
               max_budget_usd: float | None = None,
               tool_profile: str | None = None) -> tuple[int, dict, str]:
    """Run Claude Code and normalize its result plus observed tool events."""
    executable = "claude.cmd" if os.name == "nt" else "claude"
    streaming = tool_profile in {"native", "full_project"}
    command = [executable, "-p", "--no-session-persistence",
               "--output-format", "stream-json" if streaming else "json",
               "--json-schema", json.dumps(FINDING_SCHEMA)]
    if streaming:
        command.extend(["--verbose", "--bare", "--disable-slash-commands",
                        "--permission-mode", "dontAsk",
                        "--tools", "Read,Glob,Grep"])
        allowed = ["Read", "Glob", "Grep"]
        if tool_profile == "full_project":
            command.extend(["--strict-mcp-config", "--mcp-config",
                            json.dumps(_mcp_config())])
            allowed.extend(f"mcp__code-review-ai__{name}"
                           for name in ONLINE_MCP_TOOL_NAMES)
        else:
            command.extend(["--strict-mcp-config", "--mcp-config",
                            json.dumps({"mcpServers": {}})])
        command.extend(["--allowedTools", ",".join(allowed)])
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
    }
    return {"mcpServers": {"code-review-ai": {
        "type": "stdio", "command": sys.executable,
        "args": ["-m", "code_review_ai.mcp_server"], "env": env,
    }}}


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
    payload = normalize_claude_result(result_event)
    available_tools = set()
    for event in events:
        if event.get("type") == "system" and event.get("subtype") == "init":
            tools = event.get("tools")
            if isinstance(tools, list):
                available_tools.update(tool for tool in tools
                                       if isinstance(tool, str))
    calls: list[str] = []
    files: list[str] = []
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
    return payload


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


def _error_payload(stdout: str) -> dict:
    try:
        outer = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    return {"findings": [], "files_read": [], "tool_calls": [],
            "usage": _provider_usage(outer)}


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
        if payload:
            print(json.dumps(payload, ensure_ascii=False))
        print(error, file=sys.stderr)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
