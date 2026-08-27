"""Provider adapters that normalize agent CLIs to the agent-eval JSON contract."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
from contextlib import asynccontextmanager
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
        "affected_symbols": {"type": "array", "items": {"type": "string"}},
        "affected_files": {"type": "array", "items": {"type": "string"}},
        "affected_entries": {"type": "array", "items": {"type": "string"}},
        "tests": {"type": "array", "items": {"type": "string"}},
        "files_read": {"type": "array", "items": {"type": "string"}},
        "tool_calls": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["findings", "affected_symbols", "affected_files",
                 "affected_entries", "tests", "files_read", "tool_calls"],
    "additionalProperties": False,
}


MCP_TOOL_NAMES = (
    "rebuild_index", "get_impact", "get_test_impact", "find_dead_code",
    "get_change_summary", "get_change_context", "query_graph", "search_symbol",
    "get_communities", "get_community", "call_external_service",
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

_BASH_SEARCH_COMMANDS = {"rg", "grep"}
_BASH_READ_COMMANDS = {"cat", "head", "tail"}
_BASH_OPTION_ARGS = {
    "-A", "-B", "-C", "-e", "-f", "-g", "-m", "-n", "-t",
    "--after-context", "--before-context", "--byte-offset", "--color",
    "--context", "--glob", "--max-count", "--regexp", "--type",
    "--type-not", "--line-number", "--lines", "--bytes",
}
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
    streaming = tool_profile in {"native", "native_full", "full_project"}
    command = [executable, "-p", "--no-session-persistence",
               "--output-format", "stream-json" if streaming else "json",
               "--json-schema", json.dumps(FINDING_SCHEMA)]
    if streaming:
        if tool_profile == "native_full":
            # Every Claude Code built-in tool, no external MCP. --tools default
            # enables the whole built-in set; an empty strict mcp config keeps
            # product / third-party servers out. No --allowedTools restriction,
            # so dontAsk lets any built-in tool through; the eval's read-only
            # Bash denylist still applies.
            command.extend(["--verbose", "--bare", "--disable-slash-commands",
                            "--permission-mode", "dontAsk",
                            "--tools", "default",
                            "--strict-mcp-config", "--mcp-config",
                            json.dumps({"mcpServers": {}}),
                            "--disallowedTools", ",".join(DENIED_BASH_RULES)])
        else:
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


def run_scripted_agent(prompt: str, scenario: str | None = None) -> dict:
    """Deterministic full-agent-eval agent (no LLM, no claude, no network).

    Exercises the same wiring as the real claude adapter — CLI subprocess,
    eval env vars, and an MCP server subprocess connected through the stdio
    protocol — but emits a scripted finding instead of a model call, so the
    eval harness can run in CI without claude login, tokens, or a live index
    server. ``core`` scenarios call the real get_change_summary / get_impact /
    get_test_impact through an in-process MCP stdio client; ``native``
    scenarios touch only Read/Grep semantics. When ``scenario`` is omitted it
    is derived from ``CRAI_EVAL_MODE`` so one ``--agent-command`` serves both
    eval arms.
    """
    effective = scenario or _scripted_scenario_from_env()
    changed_files = _changed_files_from_prompt(prompt)
    if effective == "core":
        return asyncio.run(_scripted_core(changed_files))
    return _scripted_native(changed_files)


def _scripted_scenario_from_env() -> str:
    mode = os.environ.get("CRAI_EVAL_MODE", "")
    return "native" if mode == "native_agent" else "core"


def _changed_files_from_prompt(prompt: str) -> list[str]:
    """The repo-relative files a patch touches, from the embedded diff."""
    files: list[str] = []
    for line in prompt.splitlines():
        match = re.match(r"^diff --git a/(\S+) b/\S+", line)
        if match and match.group(1) not in files:
            files.append(match.group(1))
    return files


def _scripted_native(changed_files: list[str]) -> dict:
    """Native-arm script: Read/Grep only, no MCP tool call."""
    target = changed_files[0] if changed_files else "src/app.py"
    return {
        "findings": [{
            "file": target, "line": 1, "title": "scripted native finding",
            "description": "Deterministic finding over the changed file."}],
        "affected_symbols": [], "affected_files": [target],
        "affected_entries": [], "tests": [],
        "files_read": [target], "tool_calls": ["Read", "Grep"],
        "tool_call_count": 2,
        "tool_trace": [
            {"sequence": 1, "tool": "Read",
             "input": {"file_path": target}, "response_chars": 0},
            {"sequence": 2, "tool": "Grep",
             "input": {"pattern": "def ", "path": target},
             "response_chars": 0},
        ],
        "usage": {"input_tokens": 0, "output_tokens": 0, "estimated": True},
    }


async def _scripted_core(changed_files: list[str]) -> dict:
    """Core-arm script: drive the real graph tools over the MCP protocol."""
    calls: list[str] = []
    trace: list[dict] = []
    async with _mcp_session() as session:
        summary_args: dict[str, object] = {}
        summary_text = await _session_call(session, "get_change_summary",
                                           summary_args)
        calls.append("mcp__code-review-ai__get_change_summary")
        trace.append(_trace_record(
            1, "mcp__code-review-ai__get_change_summary", summary_args,
            summary_text))
        summary = json.loads(summary_text)
        changed = summary.get("changed_functions", [])
        symbol = changed[0]["qname"] if changed else None
        target = (changed[0]["file"] if changed
                  else (changed_files[0] if changed_files else "src/app.py"))
        if symbol:
            impact_args: dict[str, object] = {
                "symbols": [symbol], "include_call_sites": True}
            impact_text = await _session_call(session, "get_impact",
                                              impact_args)
            calls.append("mcp__code-review-ai__get_impact")
            trace.append(_trace_record(
                2, "mcp__code-review-ai__get_impact", impact_args,
                impact_text))
            impact = json.loads(impact_text)
        else:
            impact = []
    entries = sorted({entry for record in impact
                      for entry in record.get("affected_entries", [])})
    return {
        "findings": [{
            "file": target, "line": 1, "title": "scripted core finding",
            "description": "Deterministic finding over the changed file."}],
        "affected_symbols": [symbol] if symbol else [],
        "affected_files": [target],
        "affected_entries": entries,
        "tests": [], "files_read": [target],
        "tool_calls": calls, "tool_call_count": len(calls),
        "tool_trace": trace,
        "usage": {"input_tokens": 0, "output_tokens": 0, "estimated": True},
    }


def _trace_record(sequence: int, tool: str, arguments: dict[str, object],
                  response: str) -> dict:
    """One tool_trace row: input as given, response kept in full for the viewer."""
    return {
        "sequence": sequence,
        "tool": tool,
        "input": arguments,
        "response_chars": len(response),
        "response": response,
    }


@asynccontextmanager
async def _mcp_session():
    """A connected ClientSession to the eval's in-process MCP server.

    Reuses the exact server parameters the claude adapter injects via
    ``--strict-mcp-config`` (see ``_mcp_config``) so the scripted agent
    exercises the same server subprocess path.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server_config = _mcp_config()["mcpServers"]["code-review-ai"]
    env = dict(os.environ)
    env.update(server_config.get("env") or {})
    params = StdioServerParameters(
        command=server_config["command"], args=server_config["args"],
        env=env, cwd=os.getcwd(), encoding="utf-8",
        encoding_error_handler="replace")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _session_call(session, tool_name: str,
                        arguments: dict) -> str:
    result = await session.call_tool(tool_name, arguments)
    parts = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


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
    payload.update(_tool_telemetry(tool_trace))
    # ``files_read`` is a legacy field. Keep it populated with the same
    # best-effort set as ``unique_files_touched`` so existing reports stop
    # under-counting Bash reads, while ``unknown_file_access`` makes the
    # incompleteness explicit.
    payload["files_read"] = payload["unique_files_touched"]
    return payload


def _build_tool_trace(events: list[dict], available_tools: set[str]) -> list[dict]:
    """Keep ordered tool inputs and response sizes without retaining responses.

    Claude's stream does not expose reliable per-tool wall time, but tool-use
    ids let us pair each call with its tool-result payload. This is enough to
    identify repeated graph calls and oversized responses after an eval run.
    """
    result_sizes: dict[str, int] = {}
    result_errors: dict[str, bool] = {}
    result_texts: dict[str, str] = {}
    for event in events:
        for block in _typed_blocks(event, "tool_result"):
            tool_use_id = block.get("tool_use_id")
            if not isinstance(tool_use_id, str):
                continue
            result_sizes[tool_use_id] = _serialized_chars(block.get("content"))
            result_errors[tool_use_id] = block.get("is_error") is True
            result_texts[tool_use_id] = _response_text(block.get("content"))

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
            if isinstance(tool_use_id, str):
                if name.startswith("mcp__code-review-ai__"):
                    # Keep the complete graph response so the route viewer can
                    # show exactly what the tool returned, not just a size.
                    record["response"] = result_texts.get(tool_use_id, "")
                if result_errors.get(tool_use_id):
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


def _response_text(value: object) -> str:
    """Extract the tool-result payload as text, whatever its block shape.

    A tool_result carries its payload either as a plain string or as a list of
    content blocks (``{"type": "text", "text": ...}``). This normalizes both so
    the route viewer can render the complete JSON a graph tool returned.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        if parts:
            return "\n".join(parts)
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


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


def _tool_telemetry(tool_trace: list[dict]) -> dict:
    """Derive auditable tool/access counters from observed tool events.

    Bash is intentionally treated as a partial observation source. Explicit
    file operands for the small read/search allowlist are counted, but a
    command with an implicit directory/stdin/variable target is marked as
    unknown instead of disappearing from the read statistics.
    """
    read_calls = 0
    search_calls = 0
    bash_calls = 0
    native_response_chars = 0
    mcp_response_chars = 0
    files: list[str] = []
    unknown: list[dict] = []

    def add_file(value: str) -> None:
        value = value.strip().strip("'\"")
        if not value or value == "-" or _looks_like_shell_path(value):
            return
        files.append(_relative_tool_path(value))

    for trace in tool_trace:
        if not isinstance(trace, dict):
            continue
        tool = trace.get("tool")
        response_chars = trace.get("response_chars", 0)
        if not isinstance(response_chars, int):
            response_chars = 0
        if isinstance(tool, str) and tool.startswith("mcp__"):
            mcp_response_chars += response_chars
        else:
            native_response_chars += response_chars

        if tool == "Read":
            read_calls += 1
            data = trace.get("input") if isinstance(trace.get("input"), dict) else {}
            path = data.get("file_path") or data.get("path")
            if isinstance(path, str):
                add_file(path)
            else:
                _mark_unknown(unknown, trace, "Read has no file path")
        elif tool == "Grep":
            search_calls += 1
            data = trace.get("input") if isinstance(trace.get("input"), dict) else {}
            path = data.get("path")
            if isinstance(path, str) and _is_explicit_file(path):
                add_file(path)
            else:
                _mark_unknown(unknown, trace, "Grep path is implicit or not a file")
        elif tool == "Bash":
            bash_calls += 1
            data = trace.get("input") if isinstance(trace.get("input"), dict) else {}
            command = data.get("command")
            if not isinstance(command, str):
                _mark_unknown(unknown, trace, "Bash has no command")
                continue
            _classify_bash(command, trace, files, unknown)
            # The classifier returns counts because a single Bash tool call
            # may contain a pipeline or a compound command.
            read_calls += _bash_count(command, _BASH_READ_COMMANDS)
            search_calls += _bash_count(command, _BASH_SEARCH_COMMANDS)

    return {
        "read_calls": read_calls,
        "search_calls": search_calls,
        "bash_calls": bash_calls,
        "unique_files_touched": list(dict.fromkeys(files)),
        "unknown_file_access": bool(unknown),
        "unknown_file_access_details": unknown,
        "native_response_chars": native_response_chars,
        "mcp_response_chars": mcp_response_chars,
        "total_tool_calls": len(tool_trace),
    }


def _classify_bash(command: str, trace: dict, files: list[str],
                   unknown: list[dict]) -> None:
    try:
        tokens = [token.strip("'\"") for token in shlex.split(
            command, posix=os.name != "nt")]
    except ValueError:
        _mark_unknown(unknown, trace, "Bash command could not be tokenized")
        return
    if not tokens:
        _mark_unknown(unknown, trace, "Bash command is empty")
        return

    working_dir = Path.cwd()
    for segment in _bash_segments(tokens):
        if not segment:
            continue
        command_name = Path(segment[0]).name.lower()
        if command_name == "cd":
            target = segment[1] if len(segment) == 2 else None
            if not isinstance(target, str) or not target or target == "-":
                _mark_unknown(unknown, trace, "cd target is ambiguous")
            else:
                candidate = Path(target)
                if not candidate.is_absolute():
                    candidate = working_dir / candidate
                try:
                    if candidate.is_dir():
                        working_dir = candidate.resolve()
                    else:
                        _mark_unknown(unknown, trace, "cd target is not a directory")
                except OSError:
                    _mark_unknown(unknown, trace, "cd target could not be resolved")
            continue
        if command_name in _BASH_SEARCH_COMMANDS:
            operands = _bash_operands(segment, command_name)
            if not operands or any(not _is_explicit_file(path, working_dir)
                                   for path in operands):
                _mark_unknown(unknown, trace,
                              f"{command_name} has an implicit or directory target")
            for path in operands:
                if _is_explicit_file(path, working_dir):
                    files.append(_relative_bash_path(path, working_dir))
        elif command_name in _BASH_READ_COMMANDS:
            operands = _bash_operands(segment, command_name)
            if not operands or any(path == "-" or not _is_explicit_file(
                    path, working_dir)
                                   for path in operands):
                _mark_unknown(unknown, trace,
                              f"{command_name} has an implicit or non-file target")
            for path in operands:
                if _is_explicit_file(path, working_dir):
                    files.append(_relative_bash_path(path, working_dir))
        else:
            _mark_unknown(unknown, trace,
                          f"unclassified Bash command: {command_name}")


def _bash_segments(tokens: list[str]) -> list[list[str]]:
    separators = {"&&", "||", "|", ";"}
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in separators:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _bash_count(command: str, names: set[str]) -> int:
    try:
        tokens = [token.strip("'\"") for token in shlex.split(
            command, posix=os.name != "nt")]
    except ValueError:
        return 0
    return sum(Path(segment[0]).name.lower() in names
               for segment in _bash_segments(tokens) if segment)


def _bash_operands(tokens: list[str], command_name: str) -> list[str]:
    """Return positional path operands for rg/grep/cat/head/tail."""
    operands: list[str] = []
    positional: list[str] = []
    skip_next = False
    after_double_dash = False
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            after_double_dash = True
            continue
        if not after_double_dash and token.startswith("-"):
            option = token.split("=", 1)[0]
            if option in _BASH_OPTION_ARGS and "=" not in token:
                skip_next = True
            continue
        positional.append(token)

    if command_name in _BASH_SEARCH_COMMANDS:
        # rg/grep take a pattern before their file operands. `grep -f` is
        # already treated as uncertain by skipping its file argument above.
        return positional[1:] if positional else []
    if command_name in _BASH_READ_COMMANDS:
        if command_name in {"head", "tail"} and positional:
            # -n/-c values are skipped above; all remaining positionals are
            # file operands. No pattern is consumed for these commands.
            return positional
        return positional
    return []


def _is_explicit_file(value: object, base_dir: Path | None = None) -> bool:
    if not isinstance(value, str):
        return False
    value = value.strip().strip("'\"")
    if not value or value in {"-", ".", ".."}:
        return False
    if any(marker in value for marker in ("*", "?", "$", "`")):
        return False
    try:
        path = Path(value)
        if base_dir is not None and not path.is_absolute():
            path = base_dir / path
        return not path.resolve().is_dir()
    except OSError:
        return False


def _relative_bash_path(value: str, working_dir: Path) -> str:
    path = Path(value)
    if not path.is_absolute():
        path = working_dir / path
    return _relative_tool_path(str(path))


def _looks_like_shell_path(value: str) -> bool:
    return any(marker in value for marker in ("$", "`", "*", "?"))


def _mark_unknown(unknown: list[dict], trace: dict, reason: str) -> None:
    record = {"sequence": trace.get("sequence", 0), "reason": reason}
    data = trace.get("input") if isinstance(trace.get("input"), dict) else {}
    command = data.get("command")
    if isinstance(command, str):
        record["command"] = command[:500]
    if record not in unknown:
        unknown.append(record)


def _telemetry_defaults() -> dict:
    return {
        "read_calls": 0, "search_calls": 0, "bash_calls": 0,
        "unique_files_touched": [], "unknown_file_access": False,
        "unknown_file_access_details": [], "native_response_chars": 0,
        "mcp_response_chars": 0, "total_tool_calls": 0,
    }


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
    result.update(_telemetry_defaults())
    return result


_EMPTY_CONTRACT = {"findings": [], "files_read": [], "tool_calls": [],
                   "usage": {}, **_telemetry_defaults()}


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
    claude.add_argument("--model",
                        default=os.environ.get("CRAI_EVAL_MODEL"))
    claude.add_argument("--max-budget-usd", type=float)
    claude.add_argument("--tool-profile",
                        choices=["none", "native", "full_project"],
                        default=os.environ.get("CRAI_EVAL_TOOL_PROFILE", "none"))
    scripted = subparsers.add_parser(
        "scripted",
        help="deterministic agent (no LLM) for wiring/regression tests")
    scripted.add_argument("--scenario", choices=["core", "native"],
                          help="default: derived from CRAI_EVAL_MODE")
    args = parser.parse_args(argv)
    prompt = sys.stdin.read()
    if args.provider == "scripted":
        payload = run_scripted_agent(prompt, scenario=args.scenario)
        print(json.dumps(payload, ensure_ascii=False))
        return 0
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
