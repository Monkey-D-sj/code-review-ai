
import json
from code_review_ai.cli import main


def test_cli_search(tmp_path, capsys):
    # rebuild first, then search
    code = main(["rebuild", "--repo", "tests/fixtures/repo",
                 "--db", str(tmp_path / "c.db")])
    assert code == 0
    _ = capsys.readouterr()  # discard rebuild output

    code = main(["search", "login", "--repo", "tests/fixtures/repo",
                 "--db", str(tmp_path / "c.db")])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert any(d["qname"] == "auth:login" for d in out)
