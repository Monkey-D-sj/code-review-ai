import json
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

    monkeypatch.setattr(ch, "_git_diff", lambda base, files, cwd=None: ({"auth.py": [(5, 6)]}, set()))
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

    monkeypatch.setattr(ch, "_git_diff", lambda base, files, cwd=None: ({"auth.py": [(2, 3)]}, set()))
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
    monkeypatch.setattr(ch, "_git_diff", lambda base, files, cwd=None: ({"auth.py": [(1, 3)]}, set()))
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
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({"auth.py": [(6, 7)]}, set()))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"auth.py": (10, 2), "logo.png": (0, 0)})
    out = build_change_summary(cfg, _conn(tmp_path))
    assert out["summary"] == {"files_changed": 2, "lines_added": 10,
                              "lines_removed": 2, "changed_functions": 1,
                              "uncovered_changes": 1, "delete_change": 0}
    assert out["changed_functions"] == [
        {"qname": Q("auth", "login"), "kind": "function",
         "file": "auth.py", "start_line": 6, "end_line": 7, "risk": 50}]
    assert out["uncovered_changes"] == [{"file": "logo.png", "hunks": []}]
    assert out["delete_change"] == []


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
    assert out["uncovered_changes"] == []
    assert out["summary"]["uncovered_changes"] == 0
    assert out["delete_change"] == []
    assert out["summary"]["delete_change"] == 0


def test_git_diff_per_hunk_shape_and_deleted(tmp_path):
    """_git_diff returns per-hunk (start, count) and flags deleted files."""
    repo = _git_repo(tmp_path)
    _commit(repo, "a.py", "x = 1\ny = 2\nz = 3\n")
    _commit(repo, "b.py", "keep = True\n")
    (repo / "b.py").unlink()                                          # tracked deletion
    (repo / "a.py").write_text("x = 1\ny = 2\nz = 3\nw = 4\n", encoding="utf-8")
    import code_review_ai.changes as ch
    ranges, deleted = ch._git_diff("HEAD", None, str(repo))
    assert "b.py" in deleted
    assert ranges["a.py"] == [(4, 1)]   # +4,1 hunk: new-side start=4, count=1


def test_uncovered_unsupported_extension(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({"README.md": [(1, 5)]}, set()))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"README.md": (5, 0)})
    out = build_change_summary(cfg, _conn(tmp_path))
    assert out["summary"]["uncovered_changes"] == 1
    assert out["uncovered_changes"] == [
        {"file": "README.md", "hunks": [{"start": 1, "count": 5}]}]


def test_uncovered_module_level_hunk(tmp_path, monkeypatch):
    """Fixture line 5 is blank module-level — outside UserService(1-3)/login(6-7)."""
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({"auth.py": [(5, 1)]}, set()))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"auth.py": (1, 0)})
    out = build_change_summary(cfg, _conn(tmp_path))
    assert out["changed_functions"] == []
    assert out["uncovered_changes"] == [
        {"file": "auth.py", "hunks": [{"start": 5, "count": 1}]}]


def test_partial_coverage_splits_hunks(tmp_path, monkeypatch):
    """One file, two hunks: line 6 (inside login) covered, line 4 (module) not."""
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({"auth.py": [(6, 1), (4, 1)]}, set()))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"auth.py": (2, 0)})
    out = build_change_summary(cfg, _conn(tmp_path))
    assert [r["qname"] for r in out["changed_functions"]] == [Q("auth", "login")]
    assert out["uncovered_changes"] == [
        {"file": "auth.py", "hunks": [{"start": 4, "count": 1}]}]


def test_binary_file_uncovered(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({}, set()))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"logo.png": (0, 0)})
    out = build_change_summary(cfg, _conn(tmp_path))
    assert out["uncovered_changes"] == [{"file": "logo.png", "hunks": []}]


def test_deleted_file_uncovered(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({}, {"foo.py"}))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"foo.py": (0, 3)})
    out = build_change_summary(cfg, _conn(tmp_path))
    assert out["uncovered_changes"] == [{"file": "foo.py", "hunks": [], "deleted": True}]


def test_uncovered_invariant(tmp_path, monkeypatch):
    """No changed file silently drops: every numstat file is covered or listed."""
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({"auth.py": [(6, 1), (4, 1)]}, set()))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"auth.py": (2, 0), "logo.png": (0, 0)})
    out = build_change_summary(cfg, _conn(tmp_path))
    covered = {r["file"] for r in out["changed_functions"]}
    uncovered = {u["file"] for u in out["uncovered_changes"]}
    assert covered | uncovered == {"auth.py", "logo.png"}


def _seed_tombstone(conn, qname, kind, rel_file, file_deleted, upstream):
    conn.execute(
        "INSERT INTO tombstones(qname,kind,file_path,file_deleted,upstream_json)"
        " VALUES(?,?,?,?,?)",
        (qname, kind, os.path.join(FIX, rel_file),
         1 if file_deleted else 0, json.dumps(upstream)))


def test_delete_change_from_tombstone_deleted_file(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    cfg.repo_path = FIX       # tombstone file_path is seeded absolute (os.path.join(FIX, ...))
    import code_review_ai.changes as ch
    conn = _conn(tmp_path)
    _seed_tombstone(conn, "auth", "module", "auth.py", True,
                    [{"source": "app", "kind": "import",
                      "file": os.path.join(FIX, "app.py")}])
    _seed_tombstone(conn, "auth::login", "function", "auth.py", True,
                    [{"source": "app::main", "kind": "call",
                      "file": os.path.join(FIX, "app.py")}])
    conn.execute("DELETE FROM nodes WHERE file_path LIKE '%auth.py'")  # watcher 已清
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({}, {"auth.py"}))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"auth.py": (0, 3)})
    out = build_change_summary(cfg, conn)
    assert out["summary"]["delete_change"] == 2
    by_qname = {r["qname"]: r for r in out["delete_change"]}
    assert set(by_qname) == {"auth", "auth::login"}
    assert by_qname["auth"]["kind"] == "module"
    assert by_qname["auth"]["file_deleted"] is True
    assert by_qname["auth"]["file"] == "auth.py"
    assert by_qname["auth"]["upstream"] == [
        {"source": "app", "kind": "import", "file": "app.py"}]
    assert by_qname["auth::login"]["upstream"] == [
        {"source": "app::main", "kind": "call", "file": "app.py"}]
    assert out["uncovered_changes"] == []   # 被 delete_change 覆盖，不进 uncovered


def test_delete_change_from_tombstone_surviving_file(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    cfg.repo_path = FIX       # tombstone file_path is seeded absolute (os.path.join(FIX, ...))
    import code_review_ai.changes as ch
    conn = _conn(tmp_path)
    _seed_tombstone(conn, "auth::login", "function", "auth.py", False,
                    [{"source": "app::main", "kind": "call",
                      "file": os.path.join(FIX, "app.py")}])
    conn.execute("DELETE FROM nodes WHERE qualified_name='auth::login'")
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({}, set()))  # 纯删除 -> 无 hunk
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"auth.py": (0, 2)})
    out = build_change_summary(cfg, conn)
    assert out["summary"]["delete_change"] == 1
    record = out["delete_change"][0]
    assert record["qname"] == "auth::login"
    assert record["file_deleted"] is False
    assert record["file"] == "auth.py"
    assert out["uncovered_changes"] == []   # 空 hunk uncovered 条目被抑制


def test_delete_change_ignores_reaadded_qname(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    cfg.repo_path = FIX       # tombstone file_path is seeded absolute (os.path.join(FIX, ...))
    import code_review_ai.changes as ch
    conn = _conn(tmp_path)
    _seed_tombstone(conn, "auth::login", "function", "auth.py", False, [])
    # qname 已重新加入活图 -> 该 tombstone 不是当前删除
    conn.execute("INSERT INTO nodes(qualified_name,kind) VALUES('auth::login','function')")
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({}, set()))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"auth.py": (0, 2)})
    out = build_change_summary(cfg, conn)
    assert out["delete_change"] == []       # qname 仍在活图 -> 不是当前删除
    assert out["uncovered_changes"] == [{"file": "auth.py", "hunks": []}]


def _seed_risk_graph(conn):
    """a.py::target 有同模块+跨模块 caller; a.py::leaf 只有同模块 caller;
    b.py::external 无 caller。"""
    for qname, kind, file_path in [
            ("a::target", "function", "a.py"),
            ("a::leaf", "function", "a.py"),
            ("a::caller", "function", "a.py"),
            ("b::external", "function", "b.py"),
    ]:
        conn.execute("INSERT INTO nodes(qualified_name, kind, file_path) "
                     "VALUES(?,?,?)", (qname, kind, file_path))
    for source, target in [("a::caller", "a::target"),  # 同模块
                           ("b::external", "a::target"),  # 跨模块
                           ("a::caller", "a::leaf")]:    # 同模块
        conn.execute("INSERT INTO edges(source, target, kind, resolution) "
                     "VALUES(?,?,?,?)", (source, target, "call", "resolved"))


def test_assess_symbol_risk_rules(tmp_path):
    from code_review_ai.changes import assess_symbol_risk
    conn = _conn(tmp_path)
    _seed_risk_graph(conn)
    assert assess_symbol_risk(conn, "a::target") == 70      # 跨模块入边 -> 60+10
    assert assess_symbol_risk(conn, "a::leaf") == 35        # 同模块入边 -> 30+5
    assert assess_symbol_risk(conn, "b::external") == 10    # 叶子
    assert assess_symbol_risk(conn, "nope::missing") == 50  # 未解析
    assert assess_symbol_risk(conn, "a::target", deleted=True) == 90  # 删除


def test_assess_symbol_risk_caps_cross_module(tmp_path):
    from code_review_ai.changes import assess_symbol_risk
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO nodes(qualified_name, kind, file_path) "
                 "VALUES('a::hub','function','a.py')")
    for index in range(1, 6):  # 5 个跨模块 caller -> 60+50=110 -> 截断 100
        conn.execute("INSERT INTO nodes(qualified_name, kind, file_path) "
                     "VALUES(?,?,?)", (f"b::c{index}", "function", "b.py"))
        conn.execute("INSERT INTO edges(source, target, kind, resolution) "
                     "VALUES(?,?,?,?)", (f"b::c{index}", "a::hub", "call", "resolved"))
    assert assess_symbol_risk(conn, "a::hub") == 100
