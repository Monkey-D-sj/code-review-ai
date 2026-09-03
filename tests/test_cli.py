import json
import os

from conftest import FIXTURES as FIX, Q

from code_review_ai import cli
from code_review_ai.cli import main


def test_cli_search(tmp_path, capsys):
    # rebuild first, then search
    code = main(["rebuild", "--repo", FIX,
                 "--db", str(tmp_path / "c.db")])
    assert code == 0
    _ = capsys.readouterr()  # discard rebuild output

    code = main(["search", "login", "--limit", "5", "--repo", FIX,
                 "--db", str(tmp_path / "c.db")])
    assert code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    hit = next(line for line in lines if Q("auth", "login") in line)
    assert "function" in hit and "auth.py" in hit
    assert "def login" in hit  # signature 列


def test_cli_summary(tmp_path, capsys):
    code = main(["rebuild", "--repo", FIX, "--db", str(tmp_path / "c.db")])
    assert code == 0
    _ = capsys.readouterr()  # discard rebuild output
    code = main(["summary", "--symbols", Q("auth", "login"),
                 "--repo", FIX, "--db", str(tmp_path / "c.db")])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"summary", "changed_functions", "uncovered_changes",
                         "delete_change"}
    assert data["summary"]["changed_functions"] == 1
    assert data["changed_functions"][0]["qname"] == Q("auth", "login")


def test_cli_review_syncs_then_writes_agent_contract(tmp_path, monkeypatch):
    output = tmp_path / "review.json"
    calls = {}

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        cli, "sync",
        lambda config, conn, **kwargs: calls.update(synced=True, **kwargs))

    def fake_review(config, conn, **kwargs):
        calls.update(kwargs)
        return {"findings": [], "affected_symbols": [], "affected_files": [],
                "affected_entries": [], "tests": [], "files_read": [],
                "tool_calls": [], "tool_call_count": 0, "tool_trace": [],
                "usage": {}, "failure_reason": None}

    monkeypatch.setattr("code_review_ai.review_agent.runner.run_review", fake_review)
    code = main(["review", "--repo", FIX, "--db", str(tmp_path / "review.db"),
                 "--model", "fake-model", "--base-url", "http://provider/v1",
                 "--symbols", Q("auth", "login"), "--out", str(output)])

    assert code == 0
    assert calls["synced"] is True
    assert callable(calls["progress"])
    assert calls["model_name"] == "fake-model"
    assert calls["symbols"] == [Q("auth", "login")]
    assert json.loads(output.read_text(encoding="utf-8"))["failure_reason"] is None


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


def test_cli_agent_eval_dispatches_and_writes_report(tmp_path, monkeypatch):
    cases = tmp_path / "agent-cases.json"
    cases.write_text(json.dumps([{
        "id": "login-review",
        "prompt": "Review login.",
        "changed_symbols": [Q("auth", "login")],
        "gold_findings": [{"id": "bug", "file": "auth.py",
                           "keywords": ["authentication"]}],
    }]), encoding="utf-8")
    output = tmp_path / "agent-report.json"
    captured = {}

    def fake_run(config, conn, loaded_cases, command, output_dir, **kwargs):
        captured.update({"cases": loaded_cases, "command": command,
                         "output_dir": output_dir, **kwargs})
        return {"schema_version": 1, "aggregate": {}, "runs": []}

    monkeypatch.setattr(cli, "run_agent_eval", fake_run)
    code = main([
        "agent-eval", "--repo", FIX, "--db", str(tmp_path / "agent.db"),
        "--cases", str(cases), "--agent-command", 'agent --model "test model"',
        "--modes", "diff_only", "graph_agent", "--repetitions", "2",
        "--runs-dir", str(tmp_path / "runs"), "--out", str(output),
    ])

    assert code == 0
    assert captured["command"] == ["agent", "--model", "test model"]
    assert captured["modes"] == ("diff_only", "graph_agent")
    assert captured["repetitions"] == 2
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == 1


def test_cli_full_agent_eval_automatically_writes_routes(tmp_path, monkeypatch,
                                                        capsys):
    cases = tmp_path / "full-cases.json"
    cases.write_text(json.dumps([{
        "id": "real-review", "repo_name": "sample",
        "repo_url": "https://github.com/example/sample.git",
        "source_commit": "abc123", "mutation_paths": ["src/app.py"],
        "prompt": "Review it.", "gold_findings": [{
            "id": "bug", "file": "src/app.py", "keywords": ["bug"]}],
    }]), encoding="utf-8")
    output = tmp_path / "report.json"
    work_dir = tmp_path / "work"
    payload = {"schema_version": 2, "runs": [{
        "case_id": "real-review", "mode": "native_agent", "repetition": 1,
        "precision": 1, "recall": 1, "f1": 1, "elapsed_ms": 10,
        "files_read": [], "usage": {}, "tool_trace": [],
    }]}
    monkeypatch.setattr(cli, "run_full_agent_eval", lambda *a, **k: payload)

    code = main([
        "full-agent-eval", "--cases", str(cases),
        "--agent-command", "agent", "--work-dir", str(work_dir),
        "--out", str(output),
    ])

    routes = tmp_path / "report-routes.md"
    assert code == 0
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert "real-review / native_agent / run-1" in routes.read_text(
        encoding="utf-8")
    assert str(routes) in capsys.readouterr().out


def test_cli_full_agent_eval_forwards_model_via_env(tmp_path, monkeypatch):
    """--model locks the agent model: cli sets CRAI_EVAL_MODEL for the run."""
    cases = tmp_path / "full-cases.json"
    cases.write_text(json.dumps([{
        "id": "real-review", "repo_name": "sample",
        "repo_url": "https://github.com/example/sample.git",
        "source_commit": "abc123", "mutation_paths": ["src/app.py"],
        "prompt": "Review it.", "gold_findings": [{
            "id": "bug", "file": "src/app.py", "keywords": ["bug"]}],
    }]), encoding="utf-8")
    monkeypatch.delenv("CRAI_EVAL_MODEL", raising=False)
    seen = {}

    def fake_run(*args, **kwargs):
        seen["model"] = os.environ.get("CRAI_EVAL_MODEL")
        return {"schema_version": 2, "runs": []}

    monkeypatch.setattr(cli, "run_full_agent_eval", fake_run)
    code = main([
        "full-agent-eval", "--cases", str(cases),
        "--agent-command", "agent", "--work-dir", str(tmp_path / "work"),
        "--out", str(tmp_path / "report.json"),
        "--model", "deepseek-v4-flash",
    ])
    assert code == 0
    assert seen["model"] == "deepseek-v4-flash"


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


def test_cli_test_impact_paths_format(tmp_path, capsys, monkeypatch):
    """--format paths prints space-separated, shell-ready test files (forward
    slashes, no ./ prefix) instead of JSON - for `pytest $(...)` in CI."""
    import subprocess
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
                 "--repo", str(tmp_path), "--db", db, "--format", "paths"])
    assert code == 0
    out = capsys.readouterr().out.strip()
    assert out.endswith("test_prod.py")
    assert "\\" not in out
    assert "test_prod::" not in out  # not JSON in paths mode


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
                        "source": cli.DEFAULT_SOURCE,
                        "register_mcp": False}
    assert "registered ok" in capsys.readouterr().out


def test_cli_install_register_mcp_flag(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "install",
                        lambda **kwargs: captured.update(kwargs) or _Res(True, "ok"))
    assert main(["install", "--register-mcp"]) == 0
    assert captured["register_mcp"] is True


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
    assert any(symbol["qname"] == Q("util", "hash_pw") for symbol in data["symbols"])
    assert not any(symbol["qname"] == Q("app", "main") for symbol in data["symbols"])


def test_cli_dead_code_text(tmp_path, capsys):
    assert main(["rebuild", "--repo", FIX, "--db", str(tmp_path / "dc.db")]) == 0
    _ = capsys.readouterr()
    code = main(["dead-code", "--format", "text",
                 "--repo", FIX, "--db", str(tmp_path / "dc.db")])
    assert code == 0
    out = capsys.readouterr().out
    assert Q("util", "hash_pw") in out
    assert "FILE" in out
