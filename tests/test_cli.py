from conftest import FIXTURES as FIX, Q

import json
from code_review_ai.cli import main


def test_cli_search(tmp_path, capsys):
    # rebuild first, then search
    code = main(["rebuild", "--repo", FIX,
                 "--db", str(tmp_path / "c.db")])
    assert code == 0
    _ = capsys.readouterr()  # discard rebuild output

    code = main(["search", "login", "--repo", FIX,
                 "--db", str(tmp_path / "c.db")])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert any(d["qname"] == Q("auth", "login") for d in out)
    # verify end_line is present
    login = next(d for d in out if d["qname"] == Q("auth", "login"))
    assert isinstance(login["end_line"], int) and login["end_line"] > login["line"]
