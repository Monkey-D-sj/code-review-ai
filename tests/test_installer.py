from code_review_ai.installer import (
    DEFAULT_MCP_ENTRY, DEFAULT_NAME, DEFAULT_SOURCE,
    _claude_add_command, _launch_command, install,
)


def test_launch_command_uses_uvx_from_source():
    assert _launch_command(DEFAULT_SOURCE, DEFAULT_MCP_ENTRY) == [
        "uvx", "--from", DEFAULT_SOURCE, DEFAULT_MCP_ENTRY,
    ]


def test_claude_add_command_shape():
    launch = ["uvx", "--from", "SRC", "code-review-ai-mcp"]
    cmd = _claude_add_command("code-review-ai", "user", launch)
    assert cmd == [
        "claude", "mcp", "add", "code-review-ai", "-s", "user", "--",
        "uvx", "--from", "SRC", "code-review-ai-mcp",
    ]


def test_install_unsupported_platform():
    res = install(platform="cursor")
    assert res.success is False
    assert "unsupported" in res.message


def test_install_claude_not_found(monkeypatch):
    monkeypatch.setattr("code_review_ai.installer.shutil.which", lambda _: None)
    res = install()
    assert res.success is False
    assert "claude CLI not found" in res.message
    # manual command is surfaced so the user can run it themselves
    assert "claude mcp add" in res.message
    assert "uvx" in res.command


def test_install_success_runs_claude_mcp_add(monkeypatch):
    monkeypatch.setattr("code_review_ai.installer.shutil.which", lambda _: "/usr/bin/claude")
    captured = {}

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _P()

    monkeypatch.setattr("code_review_ai.installer.subprocess.run", fake_run)
    res = install()
    assert res.success is True
    assert captured["cmd"] == [
        "claude", "mcp", "add", DEFAULT_NAME, "-s", "user", "--",
        "uvx", "--from", DEFAULT_SOURCE, DEFAULT_MCP_ENTRY,
    ]


def test_install_failure_surfaces_stderr(monkeypatch):
    monkeypatch.setattr("code_review_ai.installer.shutil.which", lambda _: "/usr/bin/claude")

    class _P:
        returncode = 1
        stdout = ""
        stderr = "server already exists"

    monkeypatch.setattr("code_review_ai.installer.subprocess.run", lambda cmd, **kw: _P())
    res = install()
    assert res.success is False
    assert "server already exists" in res.message
    assert "claude mcp remove" in res.message  # remediation hint
