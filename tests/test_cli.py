import argparse
import json
from pathlib import Path

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


class FakeResult:
    """Stand-in for the loop's LoopResult, so no model is ever reached."""

    items: dict = {}
    findings: list = []
    affected_entries: list = []
    review_complete = True
    failure_reason = None
    usage: dict = {}
    tool_trace: list = []
    assistant_turns: list = []


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
    # The graph arm is the one that opens (and here creates) the index.
    assert (tmp_path / "review.db").exists()


def test_cli_review_nograph_reads_the_diff_without_touching_the_index(
        tmp_path, monkeypatch):
    """The no-index arm: no sync, no connection, no index -- just the diff."""
    output = tmp_path / "review.json"
    calls = {}
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "build_diff_text", lambda cfg, files=None: "DIFF-BODY")
    monkeypatch.setattr(cli, "sync", lambda *args, **kwargs: calls.update(synced=True))

    def fake_free_review(config, conn=None, **kwargs):
        calls["conn"] = conn
        calls.update(kwargs)
        return FakeResult()

    monkeypatch.setattr("code_review_ai.review_loop.runner.run_free_review",
                        fake_free_review)
    code = main(["review", "--arm", "nograph", "--repo", str(tmp_path),
                 "--db", str(tmp_path / "index.db"),
                 "--model", "fake-model", "--out", str(output)])

    assert code == 0
    assert "synced" not in calls
    assert calls["conn"] is None
    assert calls["diff"] == "DIFF-BODY"
    assert calls["max_turns"] is None and calls["max_total_tokens"] is None
    # A diff-only review must not create the index it says it does not need.
    assert not (tmp_path / "index.db").exists()


def test_cli_review_passes_its_budget_to_the_arm(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "build_diff_text", lambda cfg, files=None: "")
    calls = {}

    def fake_free_review(config, conn=None, **kwargs):
        calls.update(kwargs)
        return FakeResult()

    monkeypatch.setattr("code_review_ai.review_loop.runner.run_free_review",
                        fake_free_review)
    main(["review", "--arm", "nograph", "--repo", str(tmp_path),
          "--db", str(tmp_path / "i.db"), "--model", "m",
          "--max-turns", "25", "--max-tokens", "150000"])

    assert calls["max_turns"] == 25 and calls["max_total_tokens"] == 150_000


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


def test_review_accepts_a_policy_file_flag():
    args = cli._build_parser().parse_args(["review", "--policy-file", "p.md"])

    assert args.policy_file == "p.md"


def test_review_policy_file_defaults_to_none():
    args = cli._build_parser().parse_args(["review"])

    assert args.policy_file is None


def test_cli_review_passes_the_policy_file_content_to_the_arm(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "build_diff_text", lambda cfg, files=None: "")
    policy = tmp_path / "policy.md"
    policy.write_text("CUSTOM POLICY", encoding="utf-8")
    calls = {}

    def fake_free_review(config, conn=None, **kwargs):
        calls.update(kwargs)
        return FakeResult()

    monkeypatch.setattr("code_review_ai.review_loop.runner.run_free_review",
                        fake_free_review)
    code = main(["review", "--arm", "nograph", "--repo", str(tmp_path),
                 "--db", str(tmp_path / "i.db"), "--model", "m",
                 "--policy-file", str(policy)])

    assert code == 0
    assert calls["policy"] == "CUSTOM POLICY"


def test_cli_review_passes_the_policy_file_content_to_the_graph_arm(
        tmp_path, monkeypatch):
    """The graph arm is the one the rollout uses, and its built-in policy is a
    different constant (``_POLICY`` vs ``_FREE_POLICY``), so the nograph test
    above does not cover it."""
    calls = {}
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "sync", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "build_change_summary",
                        lambda *args, **kwargs: {"changed_functions": []})
    policy = tmp_path / "policy.md"
    policy.write_text("CUSTOM POLICY", encoding="utf-8")

    def fake_review(config, conn, **kwargs):
        calls.update(kwargs)
        return FakeResult()

    monkeypatch.setattr("code_review_ai.review_loop.runner.run_review", fake_review)
    code = main(["review", "--repo", str(tmp_path), "--db", str(tmp_path / "i.db"),
                 "--model", "m", "--policy-file", str(policy)])

    assert code == 0
    assert calls["policy"] == "CUSTOM POLICY"


def test_missing_policy_file_exits_2(tmp_path, monkeypatch, capsys):
    """A missing policy file is bad configuration, never a silent fallback.

    Falling back to the built-in policy would let a caller believe it injected
    one while the run used the original.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)

    code = main(["review", "--repo", str(tmp_path), "--db", str(tmp_path / "r.db"),
                 "--model", "m", "--policy-file", "does-not-exist.md"])

    assert code == 2
    assert "does-not-exist.md" in capsys.readouterr().err


def test_empty_policy_file_exits_2(tmp_path, monkeypatch, capsys):
    """An empty file is as dangerous as a missing one.

    The runner falls back with `policy or _POLICY`, so `""` is indistinguishable
    from `None`: an empty file would silently run the baseline while the caller
    believed it was evaluating an injected policy.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)
    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")

    code = main(["review", "--repo", str(tmp_path), "--db", str(tmp_path / "r.db"),
                 "--model", "m", "--policy-file", str(empty)])

    assert code == 2
    assert "empty.md" in capsys.readouterr().err


def test_empty_policy_argument_exits_2(tmp_path, monkeypatch, capsys):
    """`--policy-file ""` -- an unset shell variable -- must not silently
    select the built-in policy either.

    `Path("")` normalizes to `Path(".")`, so without an explicit guard the
    argument falls through to the `is_file()` check and reports
    `--policy-file  does not exist` (double space) -- which points at the wrong
    problem for the likeliest real-world mistake.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)

    code = main(["review", "--repo", str(tmp_path), "--db", str(tmp_path / "r.db"),
                 "--model", "m", "--policy-file", ""])

    assert code == 2
    error = capsys.readouterr().err
    assert "does not exist" not in error
    assert "empty" in error


def test_unreadable_policy_file_exits_2(tmp_path, monkeypatch, capsys):
    """An unreadable file is bad configuration -- exit 2, like every other
    `--policy-file` failure -- never a failed run.

    `PermissionError`/`OSError` is not a `ValueError`, so an unguarded
    `read_text` escapes `_cmd_review`'s handler and is caught by
    `user_facing`'s `_USER_ERRORS` instead, exiting 1: the follow-on
    workstream's rollout reads that as the agent crashing rather than as a bad
    policy file.

    The denial is raised by a `Path.read_text` that only refuses this one path,
    rather than set with `chmod`: Windows ignores `chmod 000`, so a real
    unreadable file would skip there and leave the `OSError` arm of the guard
    uncovered.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)
    blocked = tmp_path / "blocked.md"
    blocked.write_text("POLICY", encoding="utf-8")
    real_read_text = Path.read_text

    def deny_blocked(self, *args, **kwargs):
        if self == blocked:
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_blocked)

    code = main(["review", "--repo", str(tmp_path), "--db", str(tmp_path / "r.db"),
                 "--model", "m", "--policy-file", str(blocked)])

    assert code == 2
    assert "blocked.md" in capsys.readouterr().err


def test_non_utf8_policy_file_exits_2_and_names_the_file(tmp_path, monkeypatch, capsys):
    """A non-UTF-8 policy file is bad configuration, and the message must name
    the file: `UnicodeDecodeError`'s own text is a codec dump that does not."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)
    garbage = tmp_path / "nonutf8.md"
    garbage.write_bytes(b"\xff\xfe")

    code = main(["review", "--repo", str(tmp_path), "--db", str(tmp_path / "r.db"),
                 "--model", "m", "--policy-file", str(garbage)])

    assert code == 2
    assert "nonutf8.md" in capsys.readouterr().err
