import argparse
import io
import json
import sys
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

    findings: list = []
    review_complete = True
    failure_reason = None
    usage: dict = {}
    tool_trace: list = []
    assistant_turns: list = []


def test_cli_review_syncs_then_writes_agent_contract(tmp_path, monkeypatch):
    output = tmp_path / "review.json"
    calls = {}

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "build_diff_text", lambda cfg, files=None: "DIFF-BODY")
    monkeypatch.setattr(
        cli, "sync",
        lambda config, conn, **kwargs: calls.update(synced=True, **kwargs))

    def fake_review(config, conn, **kwargs):
        calls.update(kwargs)
        return FakeResult()

    monkeypatch.setattr("code_review_ai.review_loop.runner.run_review", fake_review)
    code = main(["review", "--repo", FIX, "--db", str(tmp_path / "review.db"),
                 "--model", "fake-model", "--base-url", "http://provider/v1",
                 "--out", str(output)])

    assert code == 0
    assert calls["synced"] is True
    assert callable(calls["progress"])
    assert calls["model_name"] == "fake-model"
    assert calls["diff"] == "DIFF-BODY"
    assert "tool_names" not in calls  # the graph arm offers every repo tool
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

    def fake_review(config, conn=None, **kwargs):
        calls["conn"] = conn
        calls.update(kwargs)
        return FakeResult()

    monkeypatch.setattr("code_review_ai.review_loop.runner.run_review",
                        fake_review)
    code = main(["review", "--arm", "nograph", "--repo", str(tmp_path),
                 "--db", str(tmp_path / "index.db"),
                 "--model", "fake-model", "--out", str(output)])

    assert code == 0
    assert "synced" not in calls
    assert calls["conn"] is None
    assert calls["diff"] == "DIFF-BODY"
    assert calls["tool_names"] == ["read_file", "search_code"]
    assert calls["max_turns"] is None and calls["max_total_tokens"] is None
    # A diff-only review must not create the index it says it does not need.
    assert not (tmp_path / "index.db").exists()


def test_cli_review_passes_its_budget_to_the_arm(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "build_diff_text", lambda cfg, files=None: "")
    calls = {}

    def fake_review(config, conn=None, **kwargs):
        calls.update(kwargs)
        return FakeResult()

    monkeypatch.setattr("code_review_ai.review_loop.runner.run_review",
                        fake_review)
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

    def fake_review(config, conn=None, **kwargs):
        calls.update(kwargs)
        return FakeResult()

    monkeypatch.setattr("code_review_ai.review_loop.runner.run_review",
                        fake_review)
    code = main(["review", "--arm", "nograph", "--repo", str(tmp_path),
                 "--db", str(tmp_path / "i.db"), "--model", "m",
                 "--policy-file", str(policy)])

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


def test_review_summary_flag_defaults_off():
    args = cli._build_parser().parse_args(["review"])

    assert args.summary is False


def test_cli_review_injects_the_change_summary_into_the_graph_arm(
        tmp_path, monkeypatch):
    """`--summary` builds the index's change summary and hands it to the arm
    as prompt text -- read-only orientation, not a worksheet: it names what
    changed, it does not demand a verdict per row."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "build_diff_text", lambda cfg, files=None: "DIFF-BODY")
    monkeypatch.setattr(cli, "sync", lambda *args, **kwargs: None)
    seen: dict = {}

    def fake_build(cfg, conn):
        seen["cfg"] = cfg
        return {"changed_functions": [{"qname": "m::UserModel"}]}

    monkeypatch.setattr(cli, "build_change_summary", fake_build)
    calls = {}

    def fake_review(config, conn=None, **kwargs):
        calls.update(kwargs)
        return FakeResult()

    monkeypatch.setattr("code_review_ai.review_loop.runner.run_review", fake_review)
    code = main(["review", "--repo", FIX, "--db", str(tmp_path / "r.db"),
                 "--model", "m", "--summary", "--out", str(tmp_path / "o.json")])

    assert code == 0
    assert calls["summary"] == '{"changed_functions": [{"qname": "m::UserModel"}]}'
    # Metadata-only: the default `summary_source="diff"` would attach each
    # function's own unified diff, sending the diff into the prompt twice.
    assert seen["cfg"].summary_source == "none"


def test_cli_review_builds_the_summary_after_syncing_the_index(
        tmp_path, monkeypatch):
    """Order matters, and only here is it visible: a summary built from a
    stale index would describe a tree the reviewer is not looking at."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "build_diff_text", lambda cfg, files=None: "")
    order: list[str] = []
    monkeypatch.setattr(cli, "sync",
                        lambda *args, **kwargs: order.append("sync"))

    def fake_build(cfg, conn):
        order.append("summary")
        return {"changed_functions": []}

    monkeypatch.setattr(cli, "build_change_summary", fake_build)
    monkeypatch.setattr("code_review_ai.review_loop.runner.run_review",
                        lambda *args, **kwargs: FakeResult())
    code = main(["review", "--repo", FIX, "--db", str(tmp_path / "r.db"),
                 "--model", "m", "--summary", "--out", str(tmp_path / "o.json")])

    assert code == 0
    assert order == ["sync", "summary"]


def test_cli_review_without_summary_passes_none(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "build_diff_text", lambda cfg, files=None: "")
    monkeypatch.setattr(cli, "sync", lambda *args, **kwargs: None)
    calls = {}

    def fake_review(config, conn=None, **kwargs):
        calls.update(kwargs)
        return FakeResult()

    monkeypatch.setattr("code_review_ai.review_loop.runner.run_review", fake_review)
    code = main(["review", "--repo", FIX, "--db", str(tmp_path / "r.db"),
                 "--model", "m", "--out", str(tmp_path / "o.json")])

    assert code == 0
    assert calls["summary"] is None


def test_summary_on_the_nograph_arm_exits_2(tmp_path, monkeypatch, capsys):
    """The summary comes from the index, so the no-index arm cannot build one.
    Refusing beats ignoring: silently dropping it would make the run look like
    the summary made no difference."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "build_diff_text", lambda cfg, files=None: "")
    monkeypatch.chdir(tmp_path)

    code = main(["review", "--arm", "nograph", "--repo", str(tmp_path),
                 "--db", str(tmp_path / "i.db"), "--model", "m", "--summary"])

    assert code == 2
    assert "summary" in capsys.readouterr().err


def test_json_on_stdout_survives_a_non_utf8_console(monkeypatch):
    """stdout is the CLI's machine interface, and callers decode it -- but it
    is written with the *console's* code page, not a fixed one. On a Chinese
    Windows install that is GBK, and the payload carries the model's output
    verbatim, so a single non-breaking space ends the run:

        error: 'gbk' codec can't encode character '\xa0'

    That is a real crash, not a hypothetical: it cost one case its result in a
    21-case benchmark batch, non-deterministically, depending on what the model
    happened to write.
    """
    narrow = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
    monkeypatch.setattr(sys, "stdout", narrow)
    payload = {"findings": [{"file": "a.py", "note": "width\u00a0here"}]}

    cli._write_json(payload, None)
    narrow.flush()
    printed = narrow.buffer.getvalue().decode("gbk")

    assert json.loads(printed) == payload


# ------------------------------------------------- the harness skill + review --


def _review_argv(tmp_path, *extra: str) -> list[str]:
    """A review command line that never touches an index or a model."""
    return ["review", "--arm", "nograph", "--repo", str(tmp_path),
            "--db", str(tmp_path / "i.db"), "--model", "m", *extra]


def _capture_review(monkeypatch, calls: dict) -> None:
    def fake_review(config, conn=None, **kwargs):
        calls.update(kwargs)
        return FakeResult()

    monkeypatch.setattr("code_review_ai.review_loop.runner.run_review", fake_review)


def test_review_parses_the_harness_skill_and_retrospective_flags():
    args = cli._build_parser().parse_args(
        ["review", "--harness-skill", "h.md", "--skill-review", "candidates",
         "--skill-review-model", "m2"])

    assert args.harness_skill == "h.md"
    assert args.skill_review == "candidates"
    assert args.skill_review_model == "m2"


def test_review_retrospective_flags_default_to_off():
    """Nothing about this feature is on by default: a run with no flags must
    make the same request it made before it existed."""
    args = cli._build_parser().parse_args(["review"])

    assert args.harness_skill is None
    assert args.skill_review is None
    assert args.skill_review_model is None


def test_cli_review_passes_the_harness_skill_body_to_the_arm(tmp_path, monkeypatch):
    """The body, not the file: frontmatter belongs to the platform's skill
    loader, and the second system message is the skill itself."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "build_diff_text", lambda cfg, files=None: "")
    skill = tmp_path / "harness.md"
    skill.write_text("---\nname: code-review-harness\n---\n\nONLY THE BODY\n",
                     encoding="utf-8")
    calls: dict = {}
    _capture_review(monkeypatch, calls)

    code = main(_review_argv(tmp_path, "--harness-skill", str(skill)))

    assert code == 0
    assert calls["harness_skill"] == "ONLY THE BODY"
    assert calls["skill_review"] is None


def test_cli_review_hands_the_retrospective_request_to_the_arm(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "build_diff_text", lambda cfg, files=None: "")
    skill = tmp_path / "harness.md"
    skill.write_text("HARNESS BODY", encoding="utf-8")
    out_dir = tmp_path / "candidates"
    calls: dict = {}
    _capture_review(monkeypatch, calls)

    code = main(_review_argv(tmp_path, "--harness-skill", str(skill),
                             "--skill-review", str(out_dir),
                             "--skill-review-model", "m2"))

    assert code == 0
    request = calls["skill_review"]
    assert request.out_dir == out_dir
    assert request.model == "m2"


def test_missing_harness_skill_exits_2(tmp_path, monkeypatch, capsys):
    """A skill that silently failed to load looks exactly like a skill that
    changed nothing -- the hardest result to read, and the reason
    ``--policy-file`` fails the same way."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)

    code = main(_review_argv(tmp_path, "--harness-skill", "does-not-exist.md"))

    assert code == 2
    assert "does-not-exist.md" in capsys.readouterr().err


def test_empty_harness_skill_argument_exits_2(tmp_path, monkeypatch, capsys):
    """`--harness-skill ""` -- an unset shell variable -- must not fall through
    to ``is_file()`` and blame the wrong problem."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)

    code = main(_review_argv(tmp_path, "--harness-skill", ""))

    assert code == 2
    assert "harness-skill" in capsys.readouterr().err


def test_a_harness_skill_with_no_body_exits_2(tmp_path, monkeypatch, capsys):
    """Frontmatter alone is an empty skill: injecting it would put a blank
    second system message in the request and call that a harness."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)
    skill = tmp_path / "harness.md"
    skill.write_text("---\nname: code-review-harness\n---\n", encoding="utf-8")

    code = main(_review_argv(tmp_path, "--harness-skill", str(skill)))

    assert code == 2
    assert "empty" in capsys.readouterr().err


def test_skill_review_without_a_harness_skill_exits_2(tmp_path, monkeypatch, capsys):
    """Bad configuration, not a no-op.

    The retrospective is told to revise the second system message. With none
    injected there is nothing there, and running anyway would read exactly like
    "the retrospective found nothing to change".
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)

    code = main(_review_argv(tmp_path, "--skill-review", str(tmp_path / "out")))

    assert code == 2
    assert "--harness-skill" in capsys.readouterr().err
