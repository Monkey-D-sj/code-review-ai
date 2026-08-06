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
