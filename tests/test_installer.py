import os

from code_review_ai.installer import (
    DEFAULT_MCP_ENTRY, DEFAULT_NAME, DEFAULT_SOURCE,
    MCP_DOC_END, MCP_DOC_START,
    _claude_add_command, _claude_executable, _launch_command,
    append_usage_docs, install,
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


def test_install_success_runs_claude_mcp_add(monkeypatch, tmp_path):
    # whichever path which() returns is what actually gets spawned; docs are
    # appended on success
    monkeypatch.setattr("code_review_ai.installer.shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("code_review_ai.installer.append_usage_docs",
                        lambda: tmp_path / "CLAUDE.md")
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
        "/usr/bin/claude", "mcp", "add", DEFAULT_NAME, "-s", "user", "--",
        "uvx", "--from", DEFAULT_SOURCE, DEFAULT_MCP_ENTRY,
    ]
    assert "Appended tool usage docs" in res.message


def test_install_failure_does_not_append_docs(monkeypatch):
    monkeypatch.setattr("code_review_ai.installer.shutil.which", lambda _: "/usr/bin/claude")
    doc_calls = []

    class _P:
        returncode = 1
        stdout = ""
        stderr = "server already exists"

    monkeypatch.setattr("code_review_ai.installer.subprocess.run", lambda cmd, **kw: _P())
    monkeypatch.setattr("code_review_ai.installer.append_usage_docs",
                        lambda: doc_calls.append(True) or None)
    res = install()
    assert res.success is False
    assert "server already exists" in res.message
    assert "claude mcp remove" in res.message  # remediation hint
    assert doc_calls == []  # docs only written on success


def test_claude_executable_resolves_cmd_shim_on_windows(monkeypatch):
    monkeypatch.setattr("code_review_ai.installer.os.name", "nt")
    monkeypatch.setattr(
        "code_review_ai.installer.shutil.which",
        lambda name: {"claude": "C:/nvm/nodejs/claude",
                      "claude.cmd": "C:/nvm/nodejs/claude.cmd"}.get(name))
    assert _claude_executable() == "C:/nvm/nodejs/claude.cmd"


def test_claude_executable_posix_keeps_path(monkeypatch):
    monkeypatch.setattr("code_review_ai.installer.os.name", "posix")
    monkeypatch.setattr("code_review_ai.installer.shutil.which",
                        lambda _: "/usr/local/bin/claude")
    assert _claude_executable() == "/usr/local/bin/claude"


def test_append_usage_docs_is_idempotent(monkeypatch, tmp_path):
    md = tmp_path / "CLAUDE.md"
    monkeypatch.setattr("code_review_ai.installer._global_claude_md", lambda: md)
    append_usage_docs()
    append_usage_docs()
    content = md.read_text(encoding="utf-8")
    assert content.count(MCP_DOC_START) == 1
    assert content.count(MCP_DOC_END) == 1
    assert "code-review-ai MCP tools" in content


def test_append_usage_docs_preserves_existing_content(monkeypatch, tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("<!-- CODEGRAPH_END -->\n", encoding="utf-8")
    monkeypatch.setattr("code_review_ai.installer._global_claude_md", lambda: md)
    append_usage_docs()
    content = md.read_text(encoding="utf-8")
    assert content.startswith("<!-- CODEGRAPH_END -->")
    assert MCP_DOC_START in content
