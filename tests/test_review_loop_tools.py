"""Tests for the real review_loop tools (read_file / search_code / get_impact).

Self-contained repo in a pytest tmp dir; get_impact runs against an in-memory
SQLite index so no network or fixture repo is needed.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from pydantic import ValidationError

from code_review_ai.config import load_config
from code_review_ai.db import init_schema
from code_review_ai.review_loop.tools import ImpactArgs, make_tools


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
    assert _is_error(repo["read_file"].run(path="app.py", start_line=1, end_line=999))
    assert _is_error(repo["read_file"].run(path="sub", start_line=1, end_line=1))


def test_search_code_finds_hits_and_no_matches(repo):
    hits = repo["search_code"].run(query="do_auth")
    assert "app.py:4:" in hits and "app.py:6:" in hits

    missed = repo["search_code"].run(query="no_such_thing", path=".")
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
