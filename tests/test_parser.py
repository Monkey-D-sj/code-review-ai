from code_review_ai.parser import parse_file, filter_excluded, CALL_SIMPLE, CALL_ATTRIBUTE, CALL_OTHER

from conftest import FIXTURES as FIX, Q


def test_parse_extracts_nodes():
    pf = parse_file(f"{FIX}/auth.py", FIX)
    qn = {n.qualified_name: n for n in pf.nodes}
    assert "auth" in qn and qn["auth"].kind == "module"
    assert qn[Q("auth","UserService")].kind == "class"
    auth_method = qn[Q("auth","authenticate",Q("auth","UserService"))]
    assert auth_method.kind == "method"
    assert auth_method.parent_qname == Q("auth","UserService")
    assert auth_method.signature == "def authenticate(self, user, pw) -> bool:"
    assert auth_method.start_line >= 1 and auth_method.end_line >= auth_method.start_line
    assert qn[Q("auth","login")].kind == "function"
    assert qn[Q("auth","login")].parent_qname is None


def test_parse_extracts_calls_and_imports():
    pf = parse_file(f"{FIX}/app.py", FIX)
    imp = {i.local_name: i for i in pf.imports}
    assert imp["login"].module == "auth" and imp["login"].imported_name == "login"
    assert imp["a"].module == "auth" and imp["a"].imported_name is None
    calls = {(c.target_expr, c.call_form) for c in pf.raw_calls}
    assert ("login", CALL_SIMPLE) in calls
    assert ("a.login", CALL_ATTRIBUTE) in calls
    assert ("obj.run", CALL_ATTRIBUTE) in calls
    assert ("vals[0]", CALL_OTHER) in calls
    assert all(c.source_qname == Q("app","main") for c in pf.raw_calls)


def test_filter_excluded_nested_directory_patterns():
    """A leading ``*/`` must match nested directories: ``*/alembic/*`` excludes
    ``app/alembic/env.py``, and ``*/test*`` excludes files under any test dir."""
    files = ["app/alembic/env.py", "app/alembic/versions/x.py",
             "app/tests/test_auth.py", "test_auth.py", "app/main.py"]
    assert filter_excluded(files, ["*/alembic/*"]) == ["app/tests/test_auth.py",
                                                        "test_auth.py", "app/main.py"]
    assert filter_excluded(files, ["*/test*"]) == ["app/alembic/env.py",
                                                   "app/alembic/versions/x.py", "app/main.py"]
    # top-level dist/ still excluded via the bare pattern
    assert filter_excluded(["dist/b.js", "app/main.py"], ["dist/*"]) == ["app/main.py"]


def test_list_source_files_single_git_call(tmp_path, monkeypatch):
    """list_source_files must collect every extension glob in ONE git call."""
    import subprocess
    from code_review_ai import parser
    calls = {"n": 0}
    real_run = subprocess.run

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting)
    files = parser.list_source_files(FIX, parser.SOURCE_GLOBS)
    assert calls["n"] == 1          # 一次调用拿到所有扩展
    assert "app.py" in files and "ts/app.ts" in files


def test_module_qname_strips_src_layout(tmp_path):
    pkg = tmp_path / "src" / "mypkg"
    pkg.mkdir(parents=True)
    mod = pkg / "service.py"
    mod.write_text("def login():\n    return True\n", encoding="utf-8")
    pf = parse_file(str(mod), str(tmp_path))
    assert pf.module_qname == "mypkg.service"
    assert Q("mypkg.service", "login") in {n.qualified_name for n in pf.nodes}

    init = pkg / "__init__.py"
    init.write_text("", encoding="utf-8")
    pfi = parse_file(str(init), str(tmp_path))
    assert pfi.module_qname == "mypkg"
