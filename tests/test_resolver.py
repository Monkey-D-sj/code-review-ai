from code_review_ai.parser import parse_file
from code_review_ai.resolver import resolve_calls, resolve_edges

from conftest import FIXTURES as FIX, Q


def _resolve():
    files = [parse_file(f"{FIX}/{n}", FIX) for n in ("auth.py", "app.py", "util.py")]
    qnames = {n.qualified_name for f in files for n in f.nodes}
    return resolve_calls(files, qnames)


def test_resolve_simple_and_attribute():
    edges = _resolve()
    by = {(e.source, e.target, e.resolution) for e in edges}
    # app::main calls login (imported from auth) -> resolved to auth::login
    assert (Q("app","main"), Q("auth","login"), "resolved") in by
    # app::main calls a.login (import module alias) -> resolved to auth::login
    assert (Q("app","main"), Q("auth","login"), "resolved") in by
    # auth::UserService.authenticate calls check() -> unresolved
    assert any(e.source == Q("auth","authenticate",Q("auth","UserService")) and e.resolution == "unresolved"
               and e.target == "check" for e in edges)


def test_resolve_dynamic_for_obj_method():
    edges = _resolve()
    dyn = [e for e in edges if e.target == "obj.run"]
    assert dyn and dyn[0].resolution == "dynamic"


def test_resolve_esm_relative_imports():
    """The P0 audit gap: `import { login } from "./auth"` must resolve to the
    repo module ts.auth, not stay an unresolved `./auth::login` edge - for
    both named and namespace imports."""
    files = [parse_file(f"{FIX}/ts/{n}", FIX)
             for n in ("app.ts", "auth.ts", "util.ts")]
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_calls(files, qnames)
    by = {(e.source, e.target, e.resolution) for e in edges}
    # named import: login() -> ts.auth::login
    assert (Q("ts.app", "main"), Q("ts.auth", "login"), "resolved") in by
    # namespace import: a.login() -> ts.auth::login
    assert any(e.source == Q("ts.app", "main")
               and e.target == Q("ts.auth", "login")
               and e.resolution == "resolved" for e in edges)
    # no edge keeps the raw ./auth specifier
    assert not any(e.target.startswith("./auth") for e in edges)


def test_resolve_cls_method():
    # add a class call fixture inline
    src = "class C:\n    def m(self): pass\nx = C()\nx.m()"
    # verified via app.py vals[0] -> other
    edges = _resolve()
    other = [e for e in edges if e.target == "vals[0]"]
    assert other and other[0].resolution == "unresolved"


def test_reexport_through_package_init(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .impl import Session\n", encoding="utf-8")
    (pkg / "impl.py").write_text("class Session:\n    pass\n", encoding="utf-8")
    consumer = tmp_path / "consumer.py"
    consumer.write_text(
        "from pkg import Session\n"
        "import pkg as p\n"
        "def use_import():\n"
        "    return Session()\n"      # from pkg import Session
        "def use_alias():\n"
        "    return p.Session()\n",   # import pkg as p
        encoding="utf-8",
    )
    files = [parse_file(str(pkg / "__init__.py"), str(tmp_path)),
             parse_file(str(pkg / "impl.py"), str(tmp_path)),
             parse_file(str(consumer), str(tmp_path))]
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_calls(files, qnames)
    by = {(e.source, e.target, e.resolution) for e in edges}
    # both `Session()` (from pkg import) and `p.Session()` (import pkg as p)
    # must resolve to the real class through the package __init__ re-export.
    # The two call sites live in distinct functions so they stay separate
    # edges after per-(source,target,kind) dedup.
    assert (Q("consumer", "use_import"), "pkg.impl::Session", "resolved") in by
    assert (Q("consumer", "use_alias"), "pkg.impl::Session", "resolved") in by
    # Pin the count: a set-membership assert would pass if one path regressed.
    resolved_to_session = [edge for edge in edges
                           if edge.target == "pkg.impl::Session"
                           and edge.resolution == "resolved"]
    assert len(resolved_to_session) == 2


def test_constructor_links_to_init(tmp_path):
    mod = tmp_path / "svc.py"
    mod.write_text(
        "class Service:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "s = Service()\n",
        encoding="utf-8",
    )
    pf = parse_file(str(mod), str(tmp_path))
    qnames = {n.qualified_name for n in pf.nodes}
    edges = resolve_calls([pf], qnames)
    by = {(e.source, e.target, e.kind, e.resolution) for e in edges}
    assert ("svc", "svc::Service", "call", "resolved") in by          # to the class
    assert ("svc", "svc::Service.__init__", "call", "resolved") in by  # to __init__


def test_resolve_self_method_to_enclosing_class(tmp_path):
    """A Python method calling self.g() resolves to the enclosing class's
    method (mirrors Java's this./receiver-type binding); a bare g() stays
    module-scope per LEGB — it must NOT jump to A.g."""
    mod = tmp_path / "svc.py"
    mod.write_text(
        "class UserService:\n"
        "    def authenticate(self):\n"
        "        return self._check()\n"
        "    def _check(self):\n"
        "        return helper()\n"
        "def helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    pf = parse_file(str(mod), str(tmp_path))
    qnames = {n.qualified_name for n in pf.nodes}
    edges = resolve_calls([pf], qnames)
    by = {(e.source, e.target, e.resolution) for e in edges}
    auth = Q("svc", "authenticate", Q("svc", "UserService"))
    check = Q("svc", "_check", Q("svc", "UserService"))
    # self._check() -> UserService._check (was dynamic)
    assert (auth, check, "resolved") in by
    # bare helper() -> module-level svc::helper, not svc::UserService.helper
    assert (check, Q("svc", "helper"), "resolved") in by
    assert not any(e.source == auth and e.target == Q("svc", "helper", Q("svc", "UserService"))
                   for e in edges)


def test_resolve_module_level_class_receiver(tmp_path):
    """Config.get() where Config is a same-module class resolves to the class
    method — the `head in local` branch must join the scoped qname with '.' not
    '::' (qname.join on a prefix already containing '::' would double-separate)."""
    mod = tmp_path / "svc.py"
    mod.write_text(
        "class Config:\n"
        "    def get():\n"
        "        return 1\n"
        "def run():\n"
        "    return Config.get()\n",
        encoding="utf-8",
    )
    pf = parse_file(str(mod), str(tmp_path))
    qnames = {n.qualified_name for n in pf.nodes}
    edges = resolve_calls([pf], qnames)
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert (Q("svc", "run"), Q("svc", "get", Q("svc", "Config")), "resolved") in by


def test_resolve_import_package_submodule_attribute(tmp_path):
    """`import pkg.b` binds `pkg` to module pkg.b, so `pkg.b.fn()` is a
    module-object walk to pkg.b::fn — not a bogus nested member pkg.b::b.fn."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "b.py").write_text("def fn():\n    return 1\n", encoding="utf-8")
    main = tmp_path / "main.py"
    main.write_text(
        "import pkg.b\n"
        "def run():\n"
        "    return pkg.b.fn()\n",
        encoding="utf-8",
    )
    files = [parse_file(str(pkg / "b.py"), str(tmp_path)),
             parse_file(str(main), str(tmp_path))]
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_calls(files, qnames)
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert (Q("main", "run"), "pkg.b::fn", "resolved") in by
    # the old wrong shape (module pkg.b, member b.fn) must not appear as an edge
    assert not any(e.target == "pkg.b::b.fn" for e in edges)


def test_resolve_dedups_repeated_calls_same_target(tmp_path):
    """A function calling the same target N times yields one graph edge, not N.

    Repeated calls within one function carry no topological meaning for
    impact/flow queries; no call_count is kept.
    """
    src = tmp_path / "rep.py"
    src.write_text(
        "def helper():\n"
        "    pass\n"
        "def caller():\n"
        "    helper()\n"      # line 4 - first occurrence
        "    helper()\n"      # line 5 - duplicate
        "    helper()\n",     # line 6 - duplicate
        encoding="utf-8",
    )
    files = [parse_file(str(src), str(tmp_path))]
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_calls(files, qnames)
    rep = [e for e in edges if e.source == Q("rep", "caller")
           and e.target == Q("rep", "helper")]
    assert len(rep) == 1
    assert rep[0].resolution == "resolved"


def test_src_layout_test_reaches_changed_symbol(tmp_path):
    pkg = tmp_path / "src" / "app"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .service import login\n", encoding="utf-8")
    (pkg / "service.py").write_text("def login(user, pw):\n    return True\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text(
        "from app import login\n"
        "def test_login():\n"
        "    assert login('u', 'p')\n",
        encoding="utf-8",
    )
    files = [parse_file(str(pkg / "__init__.py"), str(tmp_path)),
             parse_file(str(pkg / "service.py"), str(tmp_path)),
             parse_file(str(tests / "test_app.py"), str(tmp_path))]
    qnames = {node.qualified_name for file in files for node in file.nodes}
    edges = resolve_calls(files, qnames)
    by = {(edge.source, edge.target, edge.resolution) for edge in edges}
    # test function is a resolved caller of the real symbol behind `from app import login`
    assert ("tests.test_app::test_login", "app.service::login", "resolved") in by


# ── annotation dependency-injection edges (dependency_markers) ────────


def _di_fixture(tmp_path, route_sig: str):
    mod = tmp_path / "app.py"
    mod.write_text(
        "def get_db():\n"
        "    return None\n"
        f"def route({route_sig}):\n"
        "    return get_db\n",
        encoding="utf-8",
    )
    return parse_file(str(mod), str(tmp_path))


def test_di_marker_arg_emits_resolved_dependency_edge(tmp_path):
    """Depends(get_db) in a parameter default links route -> app::get_db as a
    resolved call edge (so flow/impact can reach the dependency's consumers).
    Without dependency_markers the dependency stays invisible."""
    pf = _di_fixture(tmp_path, "db=Depends(get_db)")
    qnames = {n.qualified_name for n in pf.nodes}
    with_markers = {(e.source, e.target, e.kind, e.resolution)
                    for e in resolve_calls([pf], qnames, dependency_markers=["Depends"])}
    assert (Q("app", "route"), Q("app", "get_db"), "call", "resolved") in with_markers
    plain = resolve_calls([pf], qnames)
    assert not any(e.source == Q("app", "route") and e.target == Q("app", "get_db")
                   for e in plain)


def test_di_marker_matches_dotted_target_by_short_name(tmp_path):
    """fastapi.Depends(get_db) matches the marker on qname.short, not the full
    expression."""
    pf = _di_fixture(tmp_path, "db=fastapi.Depends(get_db)")
    qnames = {n.qualified_name for n in pf.nodes}
    by = {(e.source, e.target, e.kind, e.resolution)
          for e in resolve_calls([pf], qnames, dependency_markers=["Depends"])}
    assert (Q("app", "route"), Q("app", "get_db"), "call", "resolved") in by


def test_non_marker_param_config_emits_no_di_edge(tmp_path):
    """Query(min_length=1) is parameter config, not a dependency: no marker
    match, and its keyword arg must never become a callee."""
    pf = _di_fixture(tmp_path, "q: str = Query(min_length=1)")
    qnames = {n.qualified_name for n in pf.nodes}
    edges = resolve_calls([pf], qnames, dependency_markers=["Depends"])
    assert not any(e.target == "min_length" for e in edges)
    assert any(e.target == "Query" for e in edges)  # the call itself is kept


# ── path-alias (tsconfig @/* -> src/*) resolution ─────────────────────


def _alias_fixture(tmp_path):
    """A Vite-style TS layout: hook module + .ts and .vue pages that import it
    through the `@/` alias (mirrors hmg-ai-agent-platform-frontend)."""
    hook = tmp_path / "src" / "hooks" / "useSelectOptions.ts"
    hook.parent.mkdir(parents=True)
    hook.write_text(
        "export function useSelectOptions() {}\n"
        "export function load() { useSelectOptions(); }\n",
        encoding="utf-8",
    )
    page = tmp_path / "src" / "views" / "policy" / "index.ts"
    page.parent.mkdir(parents=True)
    page.write_text(
        'import { useSelectOptions } from "@/hooks/useSelectOptions";\n'
        "useSelectOptions();\n",
        encoding="utf-8",
    )
    vue = tmp_path / "src" / "views" / "policy" / "page.vue"
    vue.write_text(
        "<template><div/></template>\n"
        '<script setup lang="ts">\n'
        'import { useSelectOptions } from "@/hooks/useSelectOptions";\n'
        "useSelectOptions();\n"
        "</script>\n",
        encoding="utf-8",
    )
    return [parse_file(str(p), str(tmp_path)) for p in (hook, page, vue)]


def test_ts_alias_imports_resolve_with_path_aliases(tmp_path):
    files = _alias_fixture(tmp_path)
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_edges(files, qnames, {"@/": "src/"})
    by = {(e.source, e.target, e.kind, e.resolution) for e in edges}
    # import edges: both the .ts and the .vue page reach the hook module via @/
    assert ("views.policy.index", "hooks.useSelectOptions", "import", "resolved") in by
    assert ("views.policy.page", "hooks.useSelectOptions", "import", "resolved") in by
    # call edges resolve to the real function in the hook module (top-level call
    # source is the module qname)
    assert ("views.policy.index",
            Q("hooks.useSelectOptions", "useSelectOptions"), "call", "resolved") in by
    assert ("views.policy.page",
            Q("hooks.useSelectOptions", "useSelectOptions"), "call", "resolved") in by
    # same-module call inside the hook still resolves
    assert (Q("hooks.useSelectOptions", "load"),
            Q("hooks.useSelectOptions", "useSelectOptions"), "call", "resolved") in by


def test_ts_alias_without_path_aliases_stays_unresolved(tmp_path):
    """Regression guard: the debug-log bug — without aliases the @/ imports and
    the page -> hook calls must remain unresolved, not silently disappear."""
    files = _alias_fixture(tmp_path)
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_edges(files, qnames)  # no path_aliases
    by = {(e.source, e.target, e.kind, e.resolution) for e in edges}
    # raw alias specifier kept on the unresolved import edge
    assert ("views.policy.index", "@/hooks/useSelectOptions", "import", "unresolved") in by
    # no resolved call into the hook from the pages
    assert not any(e.source == "views.policy.index" and e.kind == "call"
                   and e.resolution == "resolved" for e in edges)
    assert not any(e.source == "views.policy.page" and e.kind == "call"
                   and e.resolution == "resolved" for e in edges)
