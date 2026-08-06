"""Extract the final answer from a claude stream-json transcript.

The review hook runs `claude -p --output-format stream-json --verbose` so the
debug log captures the full flow (every tool/skill/MCP call and result). The
final answer is embedded in that stream, in the last `result` event's `result`
field (falling back to the last assistant text block). `extract_review` pulls
it back out into a plain-text review file.
"""

import json
from pathlib import Path


def extract_review(debug_path: str, out_path: str) -> bool:
    """Write the final answer from a stream-json transcript to out_path.

    Returns False (writing nothing) if no answer text was found.
    """
    answer = ""
    fallback = ""
    with open(debug_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                answer = event.get("result") or ""
            elif event.get("type") == "assistant":
                message = event.get("message") or {}
                for block in message.get("content") or []:
                    if block.get("type") == "text" and block.get("text"):
                        fallback = block["text"]
    if not answer:
        answer = fallback
    if not answer:
        return False
    Path(out_path).write_text(answer.rstrip() + "\n", encoding="utf-8")
    return True


def trace_review(debug_path: str, trace_path: str) -> int:
    """Write a one-line-per-tool trace of the flow to trace_path.

    The raw stream-json transcript is huge (system/thinking blocks drown the
    signal), so the trace keeps just one line per tool_use (skill/tool/MCP
    name + input) with the tool_result's content collapsed to one line beneath
    it, plus a final `result:` line. Values are NOT truncated — the full input
    and result are preserved for debugging. Returns the number of tool calls
    traced.
    """
    lines: list[str] = []
    tool_count = 0
    with open(debug_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type")
            if event_type == "assistant":
                for block in event.get("message", {}).get("content") or []:
                    if block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        input_text = _compact_input(block.get("input") or {})
                        entry = f"tool: {name} {input_text}".rstrip()
                        lines.append(entry)
                        tool_count += 1
            elif event_type == "user":
                for block in event.get("message", {}).get("content") or []:
                    if block.get("type") == "tool_result":
                        lines.append(f"  -> {_first_line(block.get('content'))}")
            elif event_type == "result":
                lines.append(f"result: {_first_line(event.get('result'))}")
    if not lines:
        return 0
    Path(trace_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tool_count


def _compact_input(data: dict) -> str:
    return ", ".join(f"{key}={value}" for key, value in data.items())


def _first_line(content) -> str:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(str(block.get("text", ""))
                        for block in content if isinstance(block, dict))
    else:
        text = str(content)
    return " ".join(text.split())
