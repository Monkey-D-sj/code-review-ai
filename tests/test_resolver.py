from code_review_ai.parser import parse_file
from code_review_ai.resolver import resolve_calls

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
        "a = Session()\n"
        "b = p.Session()\n",
        encoding="utf-8",
    )
    files = [parse_file(str(pkg / "__init__.py"), str(tmp_path)),
             parse_file(str(pkg / "impl.py"), str(tmp_path)),
             parse_file(str(consumer), str(tmp_path))]
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_calls(files, qnames)
    by = {(e.source, e.target, e.resolution) for e in edges}
    # both `Session()` (from pkg import) and `p.Session()` (import pkg as p)
    # must resolve to the real class through the package __init__ re-export
    assert ("consumer", "pkg.impl::Session", "resolved") in by


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
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_calls(files, qnames)
    by = {(e.source, e.target, e.resolution) for e in edges}
    # test function is a resolved caller of the real symbol behind `from app import login`
    assert ("tests.test_app::test_login", "app.service::login", "resolved") in by
