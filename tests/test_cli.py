import argparse
import json

from conftest import FIXTURES as FIX

from code_review_ai import cli
from code_review_ai.cli import main


def _subcommand_names() -> set[str]:
    """Every subcommand the parser accepts (argparse exposes this only here)."""
    actions = cli._build_parser()._actions
    subparsers = next(action for action in actions
                      if isinstance(action, argparse._SubParsersAction))
    return set(subparsers.choices)


def test_every_subcommand_is_dispatched():
    """The guard against a command existing in two places.

    Adding a subcommand means touching the parser and the handler table; before
    this test nothing failed if you only did one of them, which is how the
    dispatch chain grew a branch per command with no shared shape.
    """
    assert set(cli.COMMANDS) == _subcommand_names()


def test_user_facing_reports_expected_errors_instead_of_tracebacking(capsys):
    """One error policy for every command: `error: ...` on stderr, exit 1."""

    @cli.user_facing
    def failing(args, ctx):
        raise RuntimeError("index is missing")

    assert failing(None, None) == 1
    assert capsys.readouterr().err == "error: index is missing\n"


def test_cli_review_syncs_then_writes_agent_contract(tmp_path, monkeypatch):
    output = tmp_path / "review.json"
    calls = {}

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        cli, "sync",
        lambda config, conn, **kwargs: calls.update(synced=True, **kwargs))

    def fake_summary(config, conn, symbols=None, files=None):
        calls["symbols"] = symbols
        return {"changed_functions": []}

    monkeypatch.setattr(cli, "build_change_summary", fake_summary)

    class FakeResult:
        items: dict = {}
        findings: list = []
        affected_entries: list = []
        review_complete = True
        failure_reason = None
        usage: dict = {}
        tool_trace: list = []

    def fake_review(config, conn, **kwargs):
        calls.update(kwargs)
        return FakeResult()

    monkeypatch.setattr("code_review_ai.review_loop.runner.run_review", fake_review)
    code = main(["review", "--repo", FIX, "--db", str(tmp_path / "review.db"),
                 "--model", "fake-model", "--base-url", "http://provider/v1",
                 "--symbols", "auth::login", "--out", str(output)])

    assert code == 0
    assert calls["synced"] is True
    assert callable(calls["progress"])
    assert calls["model_name"] == "fake-model"
    assert calls["symbols"] == ["auth::login"]
    assert calls["summary"] == {"changed_functions": []}
    assert json.loads(output.read_text(encoding="utf-8"))["failure_reason"] is None


def test_review_without_a_model_exits_2(tmp_path, monkeypatch, capsys):
    """Bad configuration stays distinct from a failed run (exit 1)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CRAI_REVIEW_MODEL", raising=False)

    code = main(["review", "--repo", str(tmp_path), "--db", str(tmp_path / "r.db")])

    assert code == 2
    assert "CRAI_REVIEW_MODEL" in capsys.readouterr().err


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
