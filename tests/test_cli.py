from conftest import FIXTURES as FIX, Q

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
    lines = capsys.readouterr().out.strip().splitlines()
    assert any(Q("auth", "login") in line and "function" in line for line in lines)
