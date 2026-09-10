"""Tests for the real review_loop tools (read_file / search_code / get_impact).

Self-contained repo in a pytest tmp dir; get_impact runs against an in-memory
SQLite index so no network or fixture repo is needed.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest
from pydantic import ValidationError

from code_review_ai.config import load_config
from code_review_ai.db import init_schema
from code_review_ai.review_loop.tools import (ImpactArgs, _for_agent,
                                              _relative_file, make_tools)


@pytest.fixture()
def repo(tmp_path):
    """A config + tool set bound to a tiny throwaway repo."""
    (tmp_path / "app.py").write_text(
        "def login(user):\n"
        "    if not user:\n"
        "        return False\n"
        "    return do_auth(user)\n\n"
        "def do_auth(u):\n"
        "    return True\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.py").write_text("VALUE = 1\n", encoding="utf-8")

    config = load_config(repo_path=str(tmp_path))
    config.repo_path = str(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    tools = {tool.name: tool for tool in make_tools(config, conn)}
    yield tools
    conn.close()


def _content(run_result: str) -> object:
    """Parse a tool output: raises when it is not JSON, so errors stand out."""
    return json.loads(run_result)


def _is_error(run_result: str) -> bool:
    try:
        payload = json.loads(run_result)
    except json.JSONDecodeError:
        return False
    return payload.get("status") == "error"


def test_read_file_returns_numbered_lines(repo):
    text = repo["read_file"].run(path="app.py", start_line=1, end_line=3)
    assert text == "1: def login(user):\n2:     if not user:\n3:         return False"


def test_read_file_blocks_escaping_sensitive_and_big_ranges(repo):
    assert _is_error(repo["read_file"].run(path="../secret", start_line=1, end_line=1))
    assert _is_error(repo["read_file"].run(path=".env", start_line=1, end_line=1))
    assert _is_error(repo["read_file"].run(path="app.py", start_line=1, end_line=5000))
    assert _is_error(repo["read_file"].run(path="sub", start_line=1, end_line=1))


def test_read_file_allows_wide_ranges_up_to_the_cap(repo):
    # well past the old 200-line cap; returns the file's actual (short) lines
    text = repo["read_file"].run(path="app.py", start_line=1, end_line=400)
    assert not _is_error(text)
    assert text.startswith("1: def login(user):")


def test_search_code_finds_hits_and_no_matches(repo):
    hits = repo["search_code"].run(query="do_auth")
    assert "app.py:4:" in hits and "app.py:6:" in hits

    missed = repo["search_code"].run(query="no_such_thing", path=".")
    assert missed == "(no matches)"


def test_search_code_matches_any_of_pipe_separated_terms(repo):
    # one call, several literal names across files (the rg "a|b|c" idiom)
    hits = repo["search_code"].run(query="login|VALUE")
    assert "app.py:" in hits and "sub/x.py:1:VALUE = 1" in hits

    # neither term exists under sub/ -> no matches
    missed = repo["search_code"].run(query="login|ghost_xyz", path="sub")
    assert missed == "(no matches)"


def test_search_code_falls_back_without_rg(repo, monkeypatch):
    monkeypatch.setattr("code_review_ai.review_loop.tools.shutil.which",
                        lambda _name: None)
    hits = repo["search_code"].run(query="VALUE", path="sub")
    assert "sub/x.py:1:VALUE = 1" in hits


def test_search_code_invalid_scope_is_an_error(repo):
    assert _is_error(repo["search_code"].run(query="do_auth", path=".."))


def test_get_impact_reports_unknown_symbol_without_crashing(repo):
    payload = repo["get_impact"].run(symbols=["app::login"])
    assert isinstance(payload, str)
    assert json.loads(payload)[0]["found"] is False


def test_impact_args_require_symbols_or_files():
    with pytest.raises(ValidationError):
        ImpactArgs.model_validate({})
    ok = ImpactArgs.model_validate({"files": ["app.py"]})
    assert ok.files == ["app.py"]


# ---------------------------------------------------------------------------
# the get_impact payload as the review agent receives it
# ---------------------------------------------------------------------------

def _neighbor(repo_root, relative_path, **extra):
    """One upstream/downstream entry, with the file the index actually stores."""
    return {"qname": "app.api::caller",
            "file": os.path.join(str(repo_root), relative_path),
            "line": 12, "level": 1, **extra}


def _payload(repo_root, *, upstream=(), downstream=(), uncertainty=()):
    """A get_impact result in the library's own shape."""
    return [{
        "symbol": "app.utils.common_util::search_to_dict",
        "found": True,
        "upstream": list(upstream),
        "downstream": list(downstream),
        "affected_entries": ["app.api.v1::controller"],
        "uncertainty": list(uncertainty),
        "coverage": {"resolved_edges": 3, "dynamic_edges": 1, "truncated": False},
        "depth": {"upstream_max": 1, "downstream_max": 0},
    }]


def test_relative_file_strips_the_repo_prefix_the_index_stored(tmp_path):
    """The index stores os.path.join(repo, rel) -- here that is the mixed
    ``<root>\\app/utils/x.py`` shape a Windows indexing run writes."""
    stored = os.path.join(str(tmp_path), "app/utils/common_util.py")
    assert _relative_file(stored, str(tmp_path)) == "app/utils/common_util.py"


def test_relative_file_drops_a_leading_dot_slash():
    assert _relative_file("./app/x.py", ".") == "app/x.py"


def test_relative_file_keeps_a_path_outside_the_repo(tmp_path):
    outside = os.path.join(str(tmp_path.parent), "elsewhere", "x.py")
    repo_root = os.path.join(str(tmp_path), "repo")
    assert _relative_file(outside, repo_root) == outside.replace("\\", "/")


def test_relative_file_keeps_the_original_when_relativizing_raises(monkeypatch):
    """A repo indexed on another drive cannot be relativized; the path is
    returned normalized rather than invented."""
    def _raise(*_args, **_kwargs):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(os.path, "relpath", _raise)
    assert _relative_file("D:/other/x.py", "C:/repo") == "D:/other/x.py"


def test_for_agent_makes_evidence_files_repo_relative(tmp_path):
    payload = _payload(tmp_path, upstream=[_neighbor(tmp_path, "app/api/x.py")],
                       downstream=[_neighbor(tmp_path, "app/core/y.py")])
    entry = _for_agent(payload, str(tmp_path))[0]
    assert entry["upstream"][0]["file"] == "app/api/x.py"
    assert entry["downstream"][0]["file"] == "app/core/y.py"


def test_for_agent_explains_an_empty_downstream(tmp_path):
    entry = _for_agent(_payload(tmp_path), str(tmp_path))[0]
    assert any("downstream" in note for note in entry["notes"])


def test_for_agent_explains_an_empty_upstream(tmp_path):
    entry = _for_agent(_payload(tmp_path), str(tmp_path))[0]
    assert any("upstream" in note for note in entry["notes"])


def test_for_agent_says_uncertainty_is_not_a_todo_list(tmp_path):
    payload = _payload(tmp_path, uncertainty=[{"expression": "model_dump"}])
    entry = _for_agent(payload, str(tmp_path))[0]
    assert any("uncertainty" in note for note in entry["notes"])


def test_for_agent_adds_no_notes_when_the_evidence_is_complete(tmp_path):
    payload = _payload(tmp_path,
                       upstream=[_neighbor(tmp_path, "app/api/x.py")],
                       downstream=[_neighbor(tmp_path, "app/core/y.py")])
    assert "notes" not in _for_agent(payload, str(tmp_path))[0]


def test_for_agent_leaves_the_librarys_own_fields_untouched(tmp_path):
    payload = _payload(tmp_path, upstream=[_neighbor(tmp_path, "app/api/x.py")])
    before = json.loads(json.dumps(payload))
    entry = _for_agent(payload, str(tmp_path))[0]
    for field, value in before[0].items():
        if field not in ("upstream", "downstream"):
            assert entry[field] == value
