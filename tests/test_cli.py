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


def test_cli_benchmark_writes_report(tmp_path):
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{
        "id": "login-change",
        "changed_symbols": [Q("auth", "login")],
        "gold_files": ["auth.py", "app.py"],
    }]), encoding="utf-8")
    output = tmp_path / "report.json"

    code = main(["benchmark", "--repo", FIX,
                 "--db", str(tmp_path / "benchmark.db"),
                 "--cases", str(cases), "--top-k", "5",
                 "--out", str(output)])

    assert code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["aggregate"]["macro_patch_file_recall_at_k"] == 1.0


def test_cli_test_impact(tmp_path, capsys, monkeypatch):
    import subprocess
    # isolated repo with a test file (FIX has none). chdir into it so the
    # CLI's load_config() reads no pyproject and uses the test-friendly
    # defaults (exclude without */test*).
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prod.py").write_text(
        "def login(user, pw):\n    return user\n", encoding="utf-8")
    (tmp_path / "test_prod.py").write_text(
        "from prod import login\n\ndef test_login():\n    login('u','p')\n",
        encoding="utf-8")
    for cmd in (["git", "init"], ["git", "add", "-A"], ["git", "commit", "-m", "x"]):
        subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)
    db = str(tmp_path / "ti.db")
    assert main(["rebuild", "--repo", str(tmp_path), "--db", db]) == 0
    _ = capsys.readouterr()
    code = main(["test-impact", "--symbols", "prod::login",
                 "--repo", str(tmp_path), "--db", db])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["test_count"] == 1
    assert data["affected_tests"][0]["qname"] == "test_prod::test_login"
    assert data["affected_tests"][0]["covers"] == ["prod::login"]


def test_cli_update_and_sync(tmp_path, capsys):
    from conftest import FIXTURES as FIX
    from code_review_ai import cli
    db = str(tmp_path / "cli.db")
    # sync 空库 -> 全量
    assert cli.main(["sync", "--repo", FIX, "--db", db]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["full_rebuild"] is True and payload["flows"] > 0
    # update 无变化 -> 0 parse
    assert cli.main(["update", "--repo", FIX, "--db", db]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["parsed_files"] == 0


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


def test_cli_dead_code_json(tmp_path, capsys):
    assert main(["rebuild", "--repo", FIX, "--db", str(tmp_path / "dc.db")]) == 0
    _ = capsys.readouterr()
    code = main(["dead-code", "--repo", FIX, "--db", str(tmp_path / "dc.db")])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"symbols", "files", "meta"}
    assert any(s["qname"] == Q("util", "hash_pw") for s in data["symbols"])
    assert not any(s["qname"] == Q("app", "main") for s in data["symbols"])


def test_cli_dead_code_text(tmp_path, capsys):
    assert main(["rebuild", "--repo", FIX, "--db", str(tmp_path / "dc.db")]) == 0
    _ = capsys.readouterr()
    code = main(["dead-code", "--format", "text",
                 "--repo", FIX, "--db", str(tmp_path / "dc.db")])
    assert code == 0
    out = capsys.readouterr().out
    assert Q("util", "hash_pw") in out
    assert "FILE" in out
