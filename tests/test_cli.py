import json

from conftest import FIXTURES as FIX, Q

from code_review_ai import cli
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


def test_cli_summary(tmp_path, capsys):
    code = main(["rebuild", "--repo", FIX, "--db", str(tmp_path / "c.db")])
    assert code == 0
    _ = capsys.readouterr()  # discard rebuild output
    code = main(["summary", "--symbols", Q("auth", "login"),
                 "--repo", FIX, "--db", str(tmp_path / "c.db")])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"summary", "changed_functions"}
    assert data["summary"]["changed_functions"] == 1
    assert data["changed_functions"][0]["qname"] == Q("auth", "login")


def test_cli_query_graph(tmp_path, capsys):
    code = main(["rebuild", "--repo", FIX, "--db", str(tmp_path / "c.db")])
    assert code == 0
    _ = capsys.readouterr()
    code = main(["query-graph", Q("auth", "login"),
                 "--repo", FIX, "--db", str(tmp_path / "c.db")])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["edge_kind"] == "call"
    assert [n["qname"] for n in data["in"]] == [Q("app", "main")]


class _Res:
    def __init__(self, success, message):
        self.success = success
        self.message = message
        self.command = ["claude", "mcp", "add"]


def test_cli_install_dispatches_with_defaults(monkeypatch, capsys):
    captured = {}

    def fake_install(**kwargs):
        captured.update(kwargs)
        return _Res(True, "registered ok")

    monkeypatch.setattr(cli, "install", fake_install)
    code = main(["install", "--platform", "claude-code"])
    assert code == 0
    assert captured == {"platform": "claude-code", "scope": "user",
                        "name": "code-review-ai",
                        "source": cli.DEFAULT_SOURCE}
    assert "registered ok" in capsys.readouterr().out


def test_cli_install_returns_nonzero_on_failure(monkeypatch):
    monkeypatch.setattr(cli, "install", lambda **k: _Res(False, "nope"))
    assert main(["install"]) == 1
