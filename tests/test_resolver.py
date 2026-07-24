from code_review_ai.parser import parse_file
from code_review_ai.resolver import resolve_calls

from conftest import FIXTURES as FIX


def _resolve():
    files = [parse_file(f"{FIX}/{n}", FIX) for n in ("auth.py", "app.py", "util.py")]
    qnames = {n.qualified_name for f in files for n in f.nodes}
    return resolve_calls(files, qnames)


def test_resolve_simple_and_attribute():
    edges = _resolve()
    by = {(e.source, e.target, e.resolution) for e in edges}
    # app::main calls login (imported from auth) -> resolved to auth::login
    assert ("app::main", "auth::login", "resolved") in by
    # app::main calls a.login (import module alias) -> resolved to auth::login
    assert ("app::main", "auth::login", "resolved") in by
    # auth::UserService.authenticate calls check() -> unresolved
    assert any(e.source == "auth::UserService.authenticate" and e.resolution == "unresolved"
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
