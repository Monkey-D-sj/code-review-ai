"""The ``review`` command's JSON output contract.

``LoopResult`` -> the dict the CLI prints and writes with ``--out``. Kept next
to the loop it summarises, so the review path owns its own output shape.
"""

from __future__ import annotations


def loop_result_payload(result, model_name: str | None = None) -> dict:
    """Map a ``LoopResult`` onto the review command's JSON payload."""
    confirmed = [item for item in result.items.values()
                 if item.state == "confirmed"]
    usage = result.usage if isinstance(result.usage, dict) else {}
    return {
        "findings": [finding.model_dump() for finding in result.findings],
        "affected_symbols": [item.qname for item in confirmed],
        "affected_files": sorted({item.file for item in confirmed if item.file}),
        "affected_entries": list(result.affected_entries),
        "files_read": _files_read(result.tool_trace),
        "tool_calls": [record["tool"] for record in result.tool_trace
                       if isinstance(record.get("tool"), str)],
        "tool_call_count": len(result.tool_trace),
        "tool_trace": [dict(record) for record in result.tool_trace],
        "review_complete": result.review_complete,
        "usage": {"input_tokens": _token_count(usage, "input_tokens"),
                  "output_tokens": _token_count(usage, "output_tokens"),
                  "cache_read_input_tokens": _token_count(usage, "cache_read"),
                  "model": model_name},
        "failure_reason": result.failure_reason,
    }


def _files_read(trace) -> list[str]:
    """Repo-relative paths the loop read, in first-seen order."""
    paths: list[str] = []
    for record in trace:
        if record.get("tool") != "read_file":
            continue
        args = record.get("input")
        path = args.get("path") if isinstance(args, dict) else None
        if isinstance(path, str) and path not in paths:
            paths.append(path)
    return paths


def _token_count(usage: dict, key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) else 0


__all__ = ["loop_result_payload"]
