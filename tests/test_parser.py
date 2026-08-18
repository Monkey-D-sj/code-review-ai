from code_review_ai import qname
from code_review_ai.config import DEFAULTS
from code_review_ai.parser import parse_file, filter_excluded, is_test_node, CALL_SIMPLE, CALL_ATTRIBUTE, CALL_OTHER

from conftest import FIXTURES as FIX, Q


def test_is_test_node_matches_file_path_or_name():
    globs, names = ["*/tests/*", "test_*.py"], ["test_*"]
    # test files (path match) - any function inside is a test node
    assert is_test_node("tests/test_auth.py", "tests.test_auth::test_login", globs, names)
    assert is_test_node("app/test_users.py", "app.test_users::test_login", globs, names)
    # top-level test file caught via the bare-filename fallback
    assert is_test_node("test_auth.py", "test_auth::test_login", globs, names)
    # production file but test-style short name -> still a test (name match)
    assert is_test_node("auth.py", "auth::test_login", globs, names)
    # production file + production name -> not a test
    assert not is_test_node("auth.py", "auth::login", globs, names)
    assert not is_test_node("app/services.py", "app.services::UserService", globs, names)


def test_is_test_node_ignores_test_in_absolute_repo_path():
    """A production file under a repo whose absolute path contains 'test'
    (e.g. /home/u/test-platform/auth.py, or a pytest tmp dir named
    test_impact_*) must NOT be tagged as a test - matching is against the
    repo-relative path, not the absolute one."""
    globs, names = ["*/tests/*", "test_*.py"], ["test_*"]
    repo = "/tmp/pytest-test_impact_x"
    assert not is_test_node(f"{repo}/a.py", "a::entry", globs, names, repo)
    # but a genuine test file under that same repo IS a test
    assert is_test_node(f"{repo}/tests/test_a.py", "tests.test_a::test_a",
                        globs, names, repo)


def test_is_test_node_default_globs_skip_testish_prod_filenames():
    """Default globs must not tag a production module whose filename starts
    with "test" but lacks the underscore (e.g. testimpact.py, testhelpers.py)
    - the dogfood failure that put testimpact.py in test_files. Only
    test_*.py / */tests/* match."""
    globs, names = ["*/tests/*", "test_*.py"], ["test_*"]
    assert not is_test_node("code_review_ai/testimpact.py",
                            "code_review_ai.testimpact::get_test_impact",
                            globs, names)
    assert not is_test_node("app/testhelpers.py",
                            "app.testhelpers::helper", globs, names)
    # genuine test files still match under the same globs
    assert is_test_node("tests/test_impact.py",
                        "tests.test_impact::test_x", globs, names)
    assert is_test_node("test_auth.py", "test_auth::test_login", globs, names)


def test_is_test_node_default_globs_cover_java_and_js():
    """The P0 audit gap: out of the box the defaults must recognize the common
    Java and TS/JS test layouts, not only Python naming habits."""
    globs, names = DEFAULTS["test_globs"], DEFAULTS["test_names"]
    # Java: Maven/Gradle layout + class-name conventions
    assert is_test_node("src/test/java/com/foo/LoginTest.java",
                        "com.foo::LoginTest.checkLogin", globs, names)
    assert is_test_node("src/main/java/com/foo/LoginTests.java",
                        "com.foo::LoginTests.verify", globs, names)
    assert is_test_node("com/foo/UserServiceTest.java",
                        "com.foo::UserServiceTest.createUser", globs, names)
    # TS/JS: Jest/Vitest/Mocha patterns
    assert is_test_node("src/auth.test.ts", "src.auth::works", globs, names)
    assert is_test_node("src/auth.spec.js", "src.auth::works", globs, names)
    assert is_test_node("__tests__/auth.ts", "__tests__.auth::works", globs, names)
    assert is_test_node("src/__tests__/auth.tsx", "src.__tests__.auth::works", globs, names)
    # Python: *_test.py suffix convention
    assert is_test_node("services/auth_test.py",
                        "services.auth_test::check", globs, names)
    # production code stays production
    assert not is_test_node("src/main/java/com/foo/LoginService.java",
                            "com.foo::LoginService.login", globs, names)
    assert not is_test_node("src/auth.ts", "src.auth::login", globs, names)


def test_is_test_node_matches_test_decorators():
    """Framework annotations are a channel file/name globs can't see - JUnit
    5's @Test tags a method even in a non-test-named file."""
    globs, names = DEFAULTS["test_globs"], DEFAULTS["test_names"]
    decorators = DEFAULTS["test_decorators"]
    assert is_test_node("src/main/java/com/foo/Checks.java",
                        "com.foo::Checks.valid", globs, names,
                        decorators=["Test"], test_decorators=decorators)
    assert is_test_node("src/main/java/com/foo/Checks.java",
                        "com.foo::Checks.matrix", globs, names,
                        decorators=["Override", "ParameterizedTest"],
                        test_decorators=decorators)
    # unrelated decorators don't tag
    assert not is_test_node("src/main/java/com/foo/Checks.java",
                            "com.foo::Checks.helper", globs, names,
                            decorators=["Override"], test_decorators=decorators)


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


def test_parse_captures_call_arguments(tmp_path):
    """DI calls in parameter defaults keep their args (Depends(get_db)) so the
    resolver can link the route to the dependency."""
    mod = tmp_path / "app.py"
    mod.write_text(
        "def get_db():\n"
        "    return None\n"
        "def route(user_id: str, db=Depends(get_db, use_cache=False)):\n"
        "    return db\n",
        encoding="utf-8",
    )
    pf = parse_file(str(mod), str(tmp_path))
    depends = [c for c in pf.raw_calls if c.target_expr == "Depends"]
    assert depends and depends[0].args == ("get_db", "use_cache=False")


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


def test_relative_import_resolves_absolute_module(tmp_path):
    mod = tmp_path / "a" / "b" / "c.py"
    mod.parent.mkdir(parents=True)
    mod.write_text("from .m import y\n", encoding="utf-8")
    pf = parse_file(str(mod), str(tmp_path))
    imp = {i.local_name: i for i in pf.imports}
    assert imp["y"].module == "a.b.m"


def test_relative_import_in_init_uses_package_base(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    init = pkg / "__init__.py"
    init.write_text("from .sub import Thing\n", encoding="utf-8")
    pf = parse_file(str(init), str(tmp_path))
    imp = {i.local_name: i for i in pf.imports}
    assert imp["Thing"].module == "pkg.sub"


def test_parse_captures_module_decorators(tmp_path):
    mod = tmp_path / "web.py"
    mod.write_text(
        'import flask\n\n'
        '@app.route("/")\n'
        'def index():\n'
        '    return "ok"\n',
        encoding="utf-8")
    pf = parse_file(str(mod), str(tmp_path))
    index = next(n for n in pf.nodes if n.qualified_name == Q("web", "index"))
    assert index.decorators == ["app.route"]


def test_parse_captures_chained_and_arg_decorators(tmp_path):
    mod = tmp_path / "cli.py"
    mod.write_text(
        'import click\n\n'
        '@click.command()\n'
        '@click.option("--x")\n'
        'def run():\n'
        '    pass\n',
        encoding="utf-8")
    pf = parse_file(str(mod), str(tmp_path))
    run = next(n for n in pf.nodes if n.qualified_name == Q("cli", "run"))
    assert run.decorators == ["click.command", "click.option"]


def test_parse_captures_method_decorators(tmp_path):
    mod = tmp_path / "svc.py"
    mod.write_text(
        'class Svc:\n'
        '    @staticmethod\n'
        '    def ping():\n'
        '        return 1\n',
        encoding="utf-8")
    pf = parse_file(str(mod), str(tmp_path))
    ping = next(n for n in pf.nodes
                if n.qualified_name == Q("svc", "ping", Q("svc", "Svc")))
    assert ping.decorators == ["staticmethod"]


def test_parse_ts_captures_class_and_method_decorators(tmp_path):
    mod = tmp_path / "pets.ts"
    mod.write_text(
        '@Controller("x")\n'
        'export class Pets {\n'
        '  @Get()\n'
        '  list() { return [] }\n'
        '}\n',
        encoding="utf-8")
    pf = parse_file(str(mod), str(tmp_path))
    by_short = {qname.short(n.qualified_name): n for n in pf.nodes}
    assert by_short["Pets"].decorators == ["Controller"]
    assert by_short["list"].decorators == ["Get"]
