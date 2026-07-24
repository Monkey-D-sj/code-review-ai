from code_review_ai.parser import parse_file

FIX = "tests/fixtures/repo"


def test_parse_extracts_nodes():
    pf = parse_file(f"{FIX}/auth.py", FIX)
    qn = {n.qualified_name: n for n in pf.nodes}
    assert "auth" in qn and qn["auth"].kind == "module"
    assert qn["auth:UserService"].kind == "class"
    auth_method = qn["auth:UserService:authenticate"]
    assert auth_method.kind == "method"
    assert auth_method.parent_qname == "auth:UserService"
    assert auth_method.signature == "def authenticate(self, user, pw) -> bool:"
    assert auth_method.start_line >= 1 and auth_method.end_line >= auth_method.start_line
    assert qn["auth:login"].kind == "function"
    assert qn["auth:login"].parent_qname is None
