"""Real review tools as ``ToolSpec``s for ``review_loop``.

Self-contained on purpose: it never imports ``review_agent`` (whose ``tools``
module pulls in langgraph), so the new package stays independent until the old
one is deleted. ``read_file``/``search_code`` bound a repo path and are the same
bounded, read-only primitives the old agent exposed; ``get_impact`` answers the
one-hop blast radius of changed symbols from the persisted index.

A tool that cannot complete (path escape, too large, bad scope) returns a
machine-readable ``{"status": "error", ...}`` string, which ``loop._result_status``
classifies as ``error``; usable output returns plain text/JSON (``success``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from fnmatch import fnmatch
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from code_review_ai.changes import detect_changed_symbols
from code_review_ai.config import Config
from code_review_ai.impact import get_impact
from code_review_ai.review_loop.schemas import (
    UPDATE_REVIEW_TOOL,
    ReviewItemUpdate,
    ToolSpec,
)

_MAX_READ_LINES = 200
_MAX_READ_CHARS = 20_000
_MAX_READ_LINE_CHARS = 500
_MAX_READ_FILE_BYTES = 8 * 1024 * 1024
_MAX_QUERY_CHARS = 200
_DEFAULT_SEARCH_RESULTS = 30
_MAX_SEARCH_RESULTS = 50
_MAX_MATCH_LINE_CHARS = 500
_MAX_FALLBACK_FILES = 1_000
_MAX_FALLBACK_BYTES = 5 * 1024 * 1024
_SENSITIVE_PARTS = {".git", ".code-review-ai"}

_README = ("Read a bounded, line-numbered range of a text file inside the "
           "repository (max 200 lines / 20k chars, single files only).")
_SEARCH_DESC = ("Literal text search (ripgrep, regex unsupported) in a bounded "
                "repo path; returns file:line:text hits.")
_IMPACT_DESC = ("One-hop callers/callees and affected entries for the changed "
                "symbols (or the files' diff symbols) you name, with call-site "
                "evidence on direct neighbors.")


class _Error(Exception):
    """Carries a stable tool failure to be rendered as an error string."""


def _error_json(reason: str) -> str:
    return json.dumps({"status": "error", "error": reason}, ensure_ascii=False)


def _inside_repo(repo_root: Path, value: str, *, require_exists: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise _Error("path must be a non-empty string")
    path = Path(value)
    candidate = path.resolve() if path.is_absolute() else (repo_root / path).resolve()
    try:
        candidate.relative_to(repo_root)
    except ValueError as exc:
        raise _Error("path must stay inside the repository") from exc
    if any(part.lower() in _SENSITIVE_PARTS or part.lower() == ".env"
           for part in candidate.parts):
        raise _Error("path is not readable by the review agent")
    if require_exists and not candidate.exists():
        raise _Error("path does not exist")
    return candidate


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

class ReadFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


def _clip_line(line: str) -> str:
    if len(line) <= _MAX_READ_LINE_CHARS:
        return line
    dropped = len(line) - _MAX_READ_LINE_CHARS
    return f"{line[:_MAX_READ_LINE_CHARS]}... (+{dropped} chars truncated)"


def _render_lines(lines: list[str], first_number: int) -> str:
    """Line-numbered rendering bounded per line and in total."""
    rendered: list[str] = []
    used = 0
    for number, line in enumerate(lines, first_number):
        entry = f"{number}: {_clip_line(line)}"
        if used + len(entry) > _MAX_READ_CHARS:
            rendered.append(f"... (output truncated at {_MAX_READ_CHARS} characters)")
            break
        rendered.append(entry)
        used += len(entry) + 1
    return "\n".join(rendered)


def _run_read(repo_path: str, path: str, start_line: int, end_line: int) -> str:
    root = Path(repo_path).resolve()
    try:
        target = _inside_repo(root, path)
        if not target.is_file():
            raise _Error("path must be a file")
        if target.stat().st_size > _MAX_READ_FILE_BYTES:
            raise _Error(f"file is larger than {_MAX_READ_FILE_BYTES} bytes")
        raw = target.read_bytes()
        if b"\0" in raw:
            raise _Error("binary files cannot be read")
        if end_line < start_line:
            raise _Error("end_line must be >= start_line")
        if end_line - start_line + 1 > _MAX_READ_LINES:
            raise _Error(f"read range cannot exceed {_MAX_READ_LINES} lines")
        lines = raw.decode("utf-8", errors="strict").splitlines()
    except (_Error, OSError, UnicodeDecodeError, ValueError) as exc:
        return _error_json(str(exc))
    rendered = _render_lines(lines[start_line - 1:end_line], start_line)
    return rendered or "(no lines in requested range)"


# ---------------------------------------------------------------------------
# search_code
# ---------------------------------------------------------------------------

class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=_MAX_QUERY_CHARS)
    path: str = "."
    glob: str | None = Field(default=None, max_length=200)
    max_results: int = Field(default=_DEFAULT_SEARCH_RESULTS, ge=1, le=_MAX_SEARCH_RESULTS)


def _matches_glob(path: Path, root: Path, pattern: str) -> bool:
    relative = path.relative_to(root).as_posix()
    return fnmatch(relative, pattern) or fnmatch(path.name, pattern)


def _python_fallback_search(root: Path, target: Path, query: str, glob: str | None,
                            max_results: int, excluded: list[str]) -> str:
    """Bounded literal search when the optional rg executable is absent."""
    candidates = [target] if target.is_file() else target.rglob("*")
    matches: list[str] = []
    files_seen = total_bytes = 0
    for candidate in candidates:
        if len(matches) >= max_results or files_seen >= _MAX_FALLBACK_FILES:
            break
        try:
            safe = _inside_repo(root, str(candidate))
        except _Error:
            continue
        if not safe.is_file() or (glob is not None and not _matches_glob(safe, root, glob)):
            continue
        if any(_matches_glob(safe, root, pattern.lstrip("!")) for pattern in excluded):
            continue
        try:
            if safe.stat().st_size > _MAX_FALLBACK_BYTES - total_bytes:
                continue
            raw = safe.read_bytes()
        except OSError:
            continue
        files_seen += 1
        total_bytes += len(raw)
        if b"\0" in raw:
            continue
        relative = safe.relative_to(root).as_posix()
        for line_number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
            if query in line:
                matches.append(f"{relative}:{line_number}:{line[:_MAX_MATCH_LINE_CHARS]}")
                if len(matches) >= max_results:
                    break
    return "\n".join(matches) if matches else "(no matches)"


def _run_search(repo_path: str, query: str, path: str, glob: str | None,
                max_results: int, excluded: list[str]) -> str:
    root = Path(repo_path).resolve()
    try:
        target = _inside_repo(root, path)
        executable = shutil.which("rg")
    except _Error as exc:
        return _error_json(str(exc))
    if not executable:
        return _python_fallback_search(root, target, query, glob, max_results, excluded)
    args = [executable, "--line-number", "--no-heading", "--color", "never",
            "--fixed-strings", "--glob", "!.git/**", "--glob", "!.git",
            "--glob", "!.code-review-ai/**", "--glob", "!.code-review-ai",
            "--glob", "!.env", "--glob", "!.env/**"]
    for pattern in excluded:
        args.extend(["--glob", "!" + pattern.lstrip("!")])
    if glob:
        args.extend(["--glob", glob])
    args.extend(["--", query, str(target)])
    try:
        completed = subprocess.run(args, cwd=root, shell=False, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace",
                                   timeout=5)
    except subprocess.TimeoutExpired:
        return _error_json("ripgrep timed out after 5 seconds")
    except (OSError, ValueError) as exc:
        return _error_json(str(exc))
    if completed.returncode == 1:
        return "(no matches)"
    if completed.returncode != 0:
        return _error_json(completed.stderr.strip() or "ripgrep failed")
    lines: list[str] = []
    for raw_line in completed.stdout.splitlines()[:max_results]:
        before, separator, text = raw_line.rpartition(":")
        if not separator:
            continue
        file_part, line_separator, line_number = before.rpartition(":")
        if not line_separator:
            continue
        try:
            relative = _inside_repo(root, file_part).relative_to(root).as_posix()
        except _Error:
            continue
        lines.append(f"{relative}:{line_number}:{text[:_MAX_MATCH_LINE_CHARS]}")
    return "\n".join(lines) if lines else "(no matches)"


# ---------------------------------------------------------------------------
# get_impact
# ---------------------------------------------------------------------------

class ImpactArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str] | None = None
    files: list[str] | None = None

    @model_validator(mode="after")
    def _require_a_target(self) -> "ImpactArgs":
        if not (self.symbols or self.files):
            raise ValueError("provide symbols, or files whose diff symbols to trace")
        return self


def _run_impact(config: Config, conn, symbols: list[str] | None,
                files: list[str] | None) -> str:
    try:
        changed = detect_changed_symbols(config, symbols=symbols, files=files)
        result = get_impact(conn, changed, max_nodes_per_direction=10,
                            include_call_sites=True, max_level=1)
    except (RuntimeError, ValueError) as exc:
        return _error_json(str(exc))
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------

def update_review_tool() -> ToolSpec:
    """The worksheet updater: schema-only; the loop applies it to candidate rows."""

    def _handled(*_args, **_kwargs) -> str:
        raise AssertionError("update_review_item is applied by the loop, never run")

    return ToolSpec(
        name=UPDATE_REVIEW_TOOL,
        description="Confirm (with a finding) or dismiss (with a reason) one "
                    "candidate row of the change worksheet.",
        args_schema=ReviewItemUpdate,
        run=_handled,
    )


def make_tools(config: Config, conn) -> list[ToolSpec]:
    """Bind the three review tools to one repo/config and its index connection."""
    repo_path = str(Path(config.repo_path).resolve())

    # Optional schema fields arrive only when the model set them (the loop runs
    # ToolSpec.run with model_dump(exclude_unset=True)), so every default on the
    # args schema must be repeated as the run-call default here.
    def read_call(path: str, start_line: int, end_line: int) -> str:
        return _run_read(repo_path, path, start_line, end_line)

    def search_call(query: str, path: str = ".",
                    glob: str | None = None,
                    max_results: int = _DEFAULT_SEARCH_RESULTS) -> str:
        return _run_search(repo_path, query, path, glob, max_results,
                           excluded=list(config.exclude))

    def impact_call(symbols: list[str] | None = None,
                    files: list[str] | None = None) -> str:
        return _run_impact(config, conn, symbols, files)

    return [
        ToolSpec(name="read_file", description=_README, args_schema=ReadFileArgs,
                 run=read_call),
        ToolSpec(name="search_code", description=_SEARCH_DESC, args_schema=SearchArgs,
                 run=search_call),
        ToolSpec(name="get_impact", description=_IMPACT_DESC, args_schema=ImpactArgs,
                 run=impact_call),
    ]
