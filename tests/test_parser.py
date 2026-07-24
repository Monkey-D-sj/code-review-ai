from code_review_ai.parser import parse_file

from conftest import FIXTURES as FIX


def test_parse_extracts_nodes():
    pf = parse_file(f"{FIX}/auth.py", FIX)
    qn = {n.qualified_name: n for n in pf.nodes}
    assert "auth" in qn and qn["auth"].kind == "module"
    assert qn["auth::UserService"].kind == "class"
    auth_method = qn["auth::UserService.authenticate"]
    assert auth_method.kind == "method"
    assert auth_method.parent_qname == "auth::UserService"
    assert auth_method.signature == "def authenticate(self, user, pw) -> bool:"
    assert auth_method.start_line >= 1 and auth_method.end_line >= auth_method.start_line
    assert qn["auth::login"].kind == "function"
    assert qn["auth::login"].parent_qname is None


def test_parse_extracts_calls_and_imports():
    pf = parse_file(f"{FIX}/app.py", FIX)
    imp = {i.local_name: i for i in pf.imports}
    assert imp["login"].module == "auth" and imp["login"].imported_name == "login"
    assert imp["a"].module == "auth" and imp["a"].imported_name is None
    calls = {(c.target_expr, c.call_form) for c in pf.raw_calls}
    assert ("login", "simple") in calls
    assert ("a.login", "attribute") in calls
    assert ("obj.run", "attribute") in calls
    assert ("vals[0]", "other") in calls
    assert all(c.source_qname == "app::main" for c in pf.raw_calls)
