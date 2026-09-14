"""The ``review`` command's JSON output contract.

``LoopResult`` -> the dict the CLI prints and writes with ``--out``. Kept next
to the loop it summarises, so the review path owns its own output shape.
"""

from __future__ import annotations


def loop_result_payload(result, model_name: str | None = None,
                        summary: str | None = None) -> dict:
    """Map a ``LoopResult`` onto the review command's JSON payload.

    ``summary`` is the change summary that was injected into the request, if
    any, and is reported as a length rather than echoed: the payload's job here
    is to say what the model was given, and 0 is the baseline (diff only), so a
    consumer comparing two runs can tell which one had the summary.
    """
    usage = result.usage if isinstance(result.usage, dict) else {}
    return {
        "findings": [finding.model_dump() for finding in result.findings],
        "files_read": _files_read(result.tool_trace),
        "tool_calls": [record["tool"] for record in result.tool_trace
                       if isinstance(record.get("tool"), str)],
        "tool_call_count": len(result.tool_trace),
        "tool_trace": [dict(record) for record in result.tool_trace],
        "assistant_turns": [turn.model_dump() for turn in result.assistant_turns],
        "change_summary_chars": len(summary or ""),
        "skill_review": _skill_review(getattr(result, "skill_review", None)),
        "review_complete": result.review_complete,
        "usage": {"input_tokens": _token_count(usage, "input_tokens"),
                  "output_tokens": _token_count(usage, "output_tokens"),
                  "cache_read_input_tokens": _token_count(usage, "cache_read"),
                  "model": model_name},
        "failure_reason": result.failure_reason,
    }


def _skill_review(inner) -> dict | None:
    """The retrospective's own run, reported apart from the review it read.

    ``None`` when none ran. Its input is the parent's entire history and its
    tokens bill separately, so folding its usage into the review's would make
    two runs' costs indistinguishable -- which matters most when cost is the
    thing under comparison. ``changes`` is the reviewer's own account of what it
    edited and why; the candidate file itself holds only the revised text, so
    this is the only place that account survives.
    """
    if inner is None:
        return None
    submission = getattr(inner, "submission", None)
    usage = inner.usage if isinstance(inner.usage, dict) else {}
    return {
        "chars": len(getattr(submission, "skill", "") or ""),
        "changes": [str(change) for change in
                    (getattr(submission, "changes", None) or [])],
        "review_complete": inner.review_complete,
        "failure_reason": inner.failure_reason,
        "turn_count": len(inner.assistant_turns),
        "tool_calls": [record["tool"] for record in inner.tool_trace
                       if isinstance(record.get("tool"), str)],
        "cost": inner.cost,
        "usage": {"input_tokens": _token_count(usage, "input_tokens"),
                  "output_tokens": _token_count(usage, "output_tokens"),
                  "cache_read_input_tokens": _token_count(usage, "cache_read")},
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
