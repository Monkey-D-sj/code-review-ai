"""Bounded read-only tools made available to the LangGraph review agent."""

from __future__ import annotations

import json
from fnmatch import fnmatch
import shutil
import subprocess
from pathlib import Path

from langchain_core.tools import StructuredTool

from code_review_ai.changes import detect_changed_symbols
from code_review_ai.impact import get_impact
from code_review_ai.review_agent.registry import RegisteredTool, ToolRegistry
from code_review_ai.review_agent.schemas import FindingReport, ReviewItemUpdate

_MAX_READ_LINES = 200
_MAX_QUERY_CHARS = 200
_DEFAULT_SEARCH_RESULTS = 30
_MAX_SEARCH_RESULTS = 50
_MAX_MATCH_LINE_CHARS = 500
_MAX_FALLBACK_FILES = 1_000
_MAX_FALLBACK_BYTES = 5 * 1024 * 1024
_SENSITIVE_PARTS = {".git", ".code-review-ai"}


def _inside_repo(repo_root: Path, value: str, *, require_exists: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path must be a non-empty string")
    path = Path(value)
    candidate = path.resolve() if path.is_absolute() else (repo_root / path).resolve()
    try:
        candidate.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError("path must stay inside the repository") from exc
    if any(part.lower() in _SENSITIVE_PARTS or part.lower() == ".env"
           for part in candidate.parts):
        raise ValueError("path is not readable by the review agent")
    if require_exists and not candidate.exists():
        raise ValueError("path does not exist")
    return candidate


def _tool_error(exc: Exception, *, status: str = "error") -> str:
    """Return a stable, machine-readable tool failure without internals."""
    return json.dumps({"status": status, "error": str(exc)}, ensure_ascii=False)


def _invalid_tool_arguments(_exc: Exception) -> str:
    """Keep schema failures in the same policy-rejection protocol as paths."""
    return _tool_error(ValueError("tool arguments do not match the allowed schema"),
                       status="rejected_policy")


def read_file(repo_path: str, path: str, start_line: int, end_line: int) -> str:
    """Read a bounded, line-numbered text range from a repository file."""
    try:
        root = Path(repo_path).resolve()
        target = _inside_repo(root, path)
        if not target.is_file():
            raise ValueError("path must be a file")
        raw = target.read_bytes()
        if b"\0" in raw:
            raise ValueError("binary files cannot be read")
        if start_line < 1 or end_line < start_line:
            raise ValueError("start_line and end_line must be a valid positive range")
        if end_line - start_line + 1 > _MAX_READ_LINES:
            raise ValueError(f"read range cannot exceed {_MAX_READ_LINES} lines")
    except ValueError as exc:
        return _tool_error(exc, status="rejected_policy")
    except OSError as exc:
        return _tool_error(exc)
    try:
        lines = raw.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        return _tool_error(exc, status="rejected_policy")
    rendered = "\n".join(
        f"{number}: {line}" for number, line in
        enumerate(lines[start_line - 1:end_line], start_line))
    return rendered or "(no lines in requested range)"


def _matches_glob(path: Path, root: Path, pattern: str) -> bool:
    """Small, conservative subset of ripgrep's glob matching for fallback."""
    relative = path.relative_to(root).as_posix()
    return fnmatch(relative, pattern) or fnmatch(path.name, pattern)


def _python_fixed_search(root: Path, target: Path, query: str,
                         glob: str | None, max_results: int,
                         excluded: list[str]) -> str:
    """Bounded literal-search fallback when the optional rg executable is absent."""
    candidates = [target] if target.is_file() else target.rglob("*")
    matches: list[str] = []
    files_seen = total_bytes = 0
    for candidate in candidates:
        if len(matches) >= max_results or files_seen >= _MAX_FALLBACK_FILES:
            break
        try:
            safe = _inside_repo(root, str(candidate))
        except ValueError:
            continue
        if not safe.is_file() or (glob is not None and not _matches_glob(safe, root, glob)):
            continue
        if any(_matches_glob(safe, root, pattern.lstrip("!")) for pattern in excluded):
            continue
        try:
            size = safe.stat().st_size
            if size > _MAX_FALLBACK_BYTES - total_bytes:
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


def search_code(repo_path: str, query: str, path: str = ".",
                glob: str | None = None, max_results: int = _DEFAULT_SEARCH_RESULTS,
                excluded: list[str] | None = None) -> str:
    """Literal ripgrep search with fixed flags and a bounded response."""
    try:
        if not isinstance(query, str) or not query:
            raise ValueError("query must be non-empty")
        if len(query) > _MAX_QUERY_CHARS:
            raise ValueError(f"query cannot exceed {_MAX_QUERY_CHARS} characters")
        if not isinstance(max_results, int) or not 1 <= max_results <= _MAX_SEARCH_RESULTS:
            raise ValueError(f"max_results must be between 1 and {_MAX_SEARCH_RESULTS}")
        if glob is not None and (not isinstance(glob, str) or not glob or len(glob) > 200):
            raise ValueError("glob must be a non-empty string no longer than 200 characters")
        root = Path(repo_path).resolve()
        target = _inside_repo(root, path)
        executable = shutil.which("rg")
        if not executable:
            return _python_fixed_search(root, target, query, glob, max_results,
                                        excluded or [])
        args = [executable, "--line-number", "--no-heading", "--color", "never",
                "--fixed-strings", "--glob", "!.git/**", "--glob", "!.git",
                "--glob", "!.code-review-ai/**", "--glob", "!.code-review-ai",
                "--glob", "!.env", "--glob", "!.env/**"]
        for pattern in excluded or []:
            if isinstance(pattern, str) and pattern and len(pattern) <= 200:
                args.extend(["--glob", "!" + pattern.lstrip("!")])
        if glob:
            args.extend(["--glob", glob])
        args.extend(["--", query, str(target)])
    except ValueError as exc:
        return _tool_error(exc, status="rejected_policy")
    try:
        completed = subprocess.run(args, cwd=root, shell=False, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace",
                                   timeout=5)
        if completed.returncode == 1:
            return "(no matches)"
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "ripgrep failed")
        lines: list[str] = []
        for raw_line in completed.stdout.splitlines()[:max_results]:
            # rg uses absolute paths because target is absolute; normalize output.
            before, separator, text = raw_line.rpartition(":")
            if not separator:
                continue
            file_part, line_separator, line_number = before.rpartition(":")
            if not line_separator:
                continue
            try:
                relative = _inside_repo(root, file_part).relative_to(root).as_posix()
            except ValueError:
                continue
            lines.append(f"{relative}:{line_number}:{text[:_MAX_MATCH_LINE_CHARS]}")
        return "\n".join(lines) if lines else "(no matches)"
    except subprocess.TimeoutExpired:
        return _tool_error(RuntimeError("ripgrep timed out after 5 seconds"))
    except (OSError, RuntimeError) as exc:
        return _tool_error(exc)


def create_tool_registry(config, conn) -> ToolRegistry:
    """Create bound LangChain tools without introducing an MCP dependency."""
    repo_path = str(Path(config.repo_path).resolve())

    def impact_handler(symbols: list[str] | None = None,
                       files: list[str] | None = None,
                       for_qname: str | None = None) -> str:
        """Return one-hop callers and callees for changed symbols."""
        try:
            changed = detect_changed_symbols(config, symbols=symbols, files=files)
            result = get_impact(conn, changed, max_nodes_per_direction=10,
                                include_call_sites=True, max_level=1)
            # The report's affected_entries are filled deterministically by the
            # runner, so the model does not need the (broad, flow-derived) entry
            # list back from this tool; dropping it trims context.
            for symbol_result in result:
                symbol_result.pop("affected_entries", None)
            return json.dumps(result, ensure_ascii=False)
        except (RuntimeError, ValueError) as exc:
            return _tool_error(exc)

    def read_handler(path: str, start_line: int, end_line: int,
                     for_qname: str | None = None) -> str:
        """Read a required, bounded range of a text file inside this repository."""
        return read_file(repo_path, path, start_line, end_line)

    def search_handler(query: str, path: str = ".", glob: str | None = None,
                       max_results: int = _DEFAULT_SEARCH_RESULTS,
                       for_qname: str | None = None) -> str:
        """Search literal text in a bounded repository path; regular expressions are not supported."""
        return search_code(repo_path, query, path, glob, max_results,
                           excluded=list(config.exclude))

    def submit_handler(**report: object) -> str:
        """Submit the final structured review report. This tool ends the review."""
        return json.dumps(report, ensure_ascii=False)

    def update_review_item_handler(**update: object) -> str:
        """Confirm or dismiss one system-created changed-symbol review item.

        Evidence is recorded automatically: to confirm a candidate you must first
        have called read_file/search_code/get_impact with ``for_qname`` set to its
        qname. Do not fabricate ``evidence_refs`` — leave them empty; the graph
        keeps the real evidence trail itself.
        """
        resolution = ReviewItemUpdate.model_validate(update)
        return json.dumps({"accepted": True, "qname": resolution.qname}, ensure_ascii=False)

    return ToolRegistry([
        RegisteredTool(StructuredTool.from_function(
            impact_handler, name="get_impact", description=impact_handler.__doc__ or "",
            handle_validation_error=_invalid_tool_arguments)),
        RegisteredTool(StructuredTool.from_function(
            read_handler, name="read_file", description=read_handler.__doc__ or "",
            handle_validation_error=_invalid_tool_arguments)),
        RegisteredTool(StructuredTool.from_function(
            search_handler, name="search_code", description=search_handler.__doc__ or "",
            handle_validation_error=_invalid_tool_arguments)),
        RegisteredTool(StructuredTool.from_function(
            update_review_item_handler, name="update_review_item",
            args_schema=ReviewItemUpdate,
            description=update_review_item_handler.__doc__ or "",
            handle_validation_error=_invalid_tool_arguments)),
        RegisteredTool(StructuredTool.from_function(
            submit_handler, name="submit_review", args_schema=FindingReport,
            description=submit_handler.__doc__ or ""), kind="terminal"),
    ])
