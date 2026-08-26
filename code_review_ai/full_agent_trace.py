"""Compact Markdown rendering for full-agent-eval tool traces."""

from __future__ import annotations

import json
import re
from pathlib import Path


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _transcript_path(root: Path, run: dict) -> Path:
    return (root / str(run["case_id"]) / str(run["mode"])
            / f"run-{run['repetition']}.json")


def _repo_path(root: Path | None, run: dict) -> str | None:
    if root is None:
        return None
    path = _transcript_path(root, run)
    if not path.exists():
        return None
    value = _load(path).get("repo_path")
    return value if isinstance(value, str) else None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _relative(value: str, repo_path: str | None) -> str:
    if not repo_path:
        return value
    normalized = value.replace("\\", "/")
    root = repo_path.replace("\\", "/").rstrip("/")
    if normalized.lower().startswith(root.lower() + "/"):
        return normalized[len(root) + 1:]
    return value


def _bash(command: str, repo_path: str | None) -> str:
    if not repo_path:
        return command
    escaped = re.escape(repo_path.replace("/", "\\"))
    return re.sub(
        rf'^cd\s+["\']{escaped}["\']\s*&&\s*', "", command,
        count=1, flags=re.IGNORECASE,
    )


def _step(record: dict, repo_path: str | None) -> str:
    tool = str(record.get("tool", "?"))
    data = record.get("input")
    data = data if isinstance(data, dict) else {}
    if tool == "Read":
        path = data.get("file_path") or data.get("path") or "?"
        detail = _relative(str(path), repo_path)
        offset, limit = data.get("offset"), data.get("limit")
        if isinstance(offset, int) and isinstance(limit, int):
            detail += f":{offset}-{offset + limit - 1}"
        elif isinstance(offset, int):
            detail += f":{offset}-EOF"
        elif isinstance(limit, int):
            detail += f":1-{limit}"
    elif tool == "Bash":
        detail = _bash(str(data.get("command", "")), repo_path)
    elif tool.startswith("mcp__code-review-ai__"):
        detail = f"{tool.removeprefix('mcp__code-review-ai__')} {_json(data)}"
        tool = "MCP"
    else:
        detail = _json(data)
    suffix = f"response={record.get('response_chars', 0)} chars"
    if record.get("is_error") is True:
        suffix += ", ERROR"
    return f"{int(record.get('sequence', 0)):02d}. {tool} | {detail} | {suffix}"


def render(report: dict, transcripts_root: Path | None = None) -> str:
    """Render all report runs as compact, complete ordered tool routes."""
    lines = ["# Full-agent execution routes", ""]
    for run in report.get("runs", []):
        if not isinstance(run, dict):
            continue
        repo_path = _repo_path(transcripts_root, run)
        usage = run.get("usage") if isinstance(run.get("usage"), dict) else {}
        trace = run.get("tool_trace") if isinstance(run.get("tool_trace"), list) else []
        lines.extend([
            f"## {run.get('case_id')} / {run.get('mode')} / run-{run.get('repetition')}",
            "",
        ])
        if repo_path:
            lines.extend([f"- cwd: `{repo_path}`", ""])
        lines.extend([
            f"- score: P={run.get('precision')} R={run.get('recall')} F1={run.get('f1')}",
            f"- elapsed: {run.get('elapsed_ms')} ms; calls: {len(trace)}; files: {len(run.get('files_read', []))}",
            f"- access: read={run.get('read_calls', 0)}, search={run.get('search_calls', 0)}, bash={run.get('bash_calls', 0)}, unique_files={len(run.get('unique_files_touched', run.get('files_read', [])))}, unknown_file_access={run.get('unknown_file_access', False)}",
            f"- responses: native={run.get('native_response_chars', 0)} chars; mcp={run.get('mcp_response_chars', 0)} chars; total_tool_calls={run.get('total_tool_calls', run.get('tool_call_count', len(trace)))}",
            f"- tokens: input={usage.get('input_tokens', 0)}, cache_read={usage.get('cache_read_input_tokens', 0)}, output={usage.get('output_tokens', 0)}; cost=${usage.get('total_cost_usd', 0)}",
            "",
            "```text",
        ])
        lines.extend(_step(item, repo_path) for item in trace
                     if isinstance(item, dict))
        lines.extend(["```", ""])
    return "\n".join(lines)


def summarize_file(report_path: str | Path,
                   transcripts_root: str | Path | None = None,
                   out_path: str | Path | None = None) -> str:
    """Render a report and optionally write it; return the Markdown text."""
    root = Path(transcripts_root) if transcripts_root is not None else None
    output = render(_load(Path(report_path)), root)
    if out_path is not None:
        Path(out_path).write_text(output, encoding="utf-8")
    return output
