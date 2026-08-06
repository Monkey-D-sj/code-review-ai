import os
import subprocess

import pytest

from code_review_ai.config import load_config
from code_review_ai.changes import (_resolve_diff_base,
                                    build_change_summary, detect_changed_symbols)

from conftest import FIXTURES as FIX, Q


def _git_repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    return repo


def _cfg(repo):
    cfg = load_config()
    cfg.repo_path = str(repo)
    cfg.diff_base = "origin/main"
    return cfg


def _commit(repo, name, content):
    (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", name], check=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "commit"],
                   check=True, env=env)


def test_resolve_diff_base_honors_explicit_config(tmp_path):
    repo = _git_repo(tmp_path)
    _commit(repo, "a.py", "x = 1")
    subprocess.run(["git", "-C", str(repo), "update-ref",
                    "refs/remotes/custom/main", "HEAD"], check=True)
    cfg = _cfg(repo)
    cfg.diff_base = "custom/main"
    assert _resolve_diff_base(cfg) == "custom/main"


def test_resolve_diff_base_uses_branch_upstream_over_default(tmp_path):
    """The default origin/main is ignored; the current branch's upstream wins."""
    repo = _git_repo(tmp_path)
    _commit(repo, "a.py", "x = 1")
    _commit(repo, "b.py", "y = 2")
    subprocess.run(["git", "-C", str(repo), "update-ref",
                    "refs/remotes/origin/main", "HEAD"], check=True)
    subprocess.run(["git", "-C", str(repo), "branch", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "branch",
                    "--set-upstream-to=main", "master"], check=True)
    assert _resolve_diff_base(_cfg(repo)) == "@{upstream}"


def test_resolve_diff_base_falls_back_to_head_parent(tmp_path):
    repo = _git_repo(tmp_path)
    _commit(repo, "a.py", "x = 1")
    _commit(repo, "b.py", "y = 2")
    assert _resolve_diff_base(_cfg(repo)) == "HEAD^"


def test_resolve_diff_base_gives_up_when_no_commits(tmp_path):
    repo = _git_repo(tmp_path)
    assert _resolve_diff_base(_cfg(repo)) == "origin/main"


def _conn(tmp_path):
    from code_review_ai.db import connect, init_schema
    conn = connect(str(tmp_path / "m.db"))
    init_schema(conn)
    return conn


def test_symbols_mode_passthrough():
    cfg = load_config(FIX)
    out = detect_changed_symbols(cfg, symbols=[Q("auth","login")])
    assert out == [Q("auth","login")]


def test_files_mode_uses_git_diff(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    # stub git diff to report a hunk on lines 5-6 of auth.py
    import code_review_ai.changes as ch

    monkeypatch.setattr(ch, "_git_diff", lambda base, files, cwd=None: {"auth.py": [(5, 6)]})
    out = detect_changed_symbols(cfg, files=["auth.py"])
    # authenticate() spans lines 2-3 in fixture; login() lines 6-7 -> line 6 hits login
    assert Q("auth","login") in out


def test_git_diff_failure_is_surfaced_not_swallowed(monkeypatch):
    """A bad diff_base must raise, not silently return an empty list."""
    cfg = load_config(FIX)
    import code_review_ai.changes as ch

    def bad_diff(base, files, cwd=None):
        raise RuntimeError("git diff failed (exit 128): fatal: bad revision 'origin/main'")

    monkeypatch.setattr(ch, "_git_diff", bad_diff)
    with pytest.raises(RuntimeError, match="bad revision"):
        detect_changed_symbols(cfg, files=["auth.py"])


def test_deleted_symbol_reported(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch

    monkeypatch.setattr(ch, "_git_diff", lambda base, files, cwd=None: {"auth.py": [(2, 3)]})
    out = detect_changed_symbols(cfg, files=["auth.py"])
    assert Q("auth","authenticate",Q("auth","UserService")) in out


def test_git_numstat_parses_text_and_binary(monkeypatch):
    import code_review_ai.changes as ch

    class _FakeResult:
        returncode = 0
        stdout = "10\t2\tauth.py\n-\t-\tlogo.png\n"
        stderr = ""
    monkeypatch.setattr(ch.subprocess, "run", lambda *args, **kwargs: _FakeResult())
    assert ch._git_numstat("origin/main") == {"auth.py": (10, 2), "logo.png": (0, 0)}


def test_git_numstat_runs_in_repo_path(tmp_path):
    """git diff must run in repo_path, not the process cwd (which is a
    different repo with its own uncommitted changes)."""
    repo = _git_repo(tmp_path)
    _commit(repo, "a.py", "x = 1")
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    import code_review_ai.changes as ch
    assert ch._git_numstat("HEAD", None, str(repo)) == {"a.py": (1, 1)}


def test_changed_functions_includes_class():
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    records = ch._changed_functions(cfg, {"auth.py": [(1, 1)]})
    user_service = [r for r in records if r["qname"] == Q("auth", "UserService")]
    assert len(user_service) == 1
    assert user_service[0]["kind"] == "class"
    assert user_service[0]["file"] == "auth.py"
    assert user_service[0]["start_line"] == 1
    assert user_service[0]["end_line"] == 3


def test_detect_changed_symbols_still_excludes_classes(monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff", lambda base, files, cwd=None: {"auth.py": [(1, 3)]})
    out = detect_changed_symbols(cfg, files=["auth.py"])
    assert Q("auth", "UserService") not in out               # class excluded
    assert Q("auth", "authenticate", Q("auth", "UserService")) in out  # method kept


def test_changed_functions_skips_unsupported_files(monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch

    def unsupported_extension(file_path, repo_root, lang=None):
        raise ValueError(f"unsupported file extension: {file_path}")

    monkeypatch.setattr(ch, "parse_file", unsupported_extension)
    assert ch._changed_functions(cfg, {"README.md": [(1, 5)]}) == []


def test_build_change_summary_diff_path(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff", lambda base, files, cwd=None: {"auth.py": [(6, 7)]})
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"auth.py": (10, 2), "logo.png": (0, 0)})
    out = build_change_summary(cfg, _conn(tmp_path))
    assert out["summary"] == {"files_changed": 2, "lines_added": 10,
                              "lines_removed": 2, "changed_functions": 1}
    assert out["changed_functions"] == [
        {"qname": Q("auth", "login"), "kind": "function",
         "file": "auth.py", "start_line": 6, "end_line": 7}]


def test_build_change_summary_symbols_path(tmp_path):
    cfg = load_config(FIX)
    cfg.repo_path = FIX
    from code_review_ai.db import connect, init_schema
    from code_review_ai.indexer import rebuild
    conn = connect(str(tmp_path / "m.db"))
    init_schema(conn)
    rebuild(cfg, conn)
    out = build_change_summary(cfg, conn, symbols=[Q("auth", "login")])
    assert out["summary"]["changed_functions"] == 1
    record = out["changed_functions"][0]
    assert record["qname"] == Q("auth", "login")
    assert record["file"] == "auth.py"
    assert record["start_line"] == 6
    assert record["end_line"] == 7
