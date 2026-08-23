"""Compatibility script for ``code-review-ai summarize-full-agent-trace``."""

from __future__ import annotations

import argparse
from pathlib import Path

from code_review_ai.full_agent_trace import summarize_file


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--transcripts-root", type=Path)
    parser.add_argument("-o", "--out", type=Path)
    args = parser.parse_args()
    output = summarize_file(args.report, args.transcripts_root, args.out)
    if not args.out:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
