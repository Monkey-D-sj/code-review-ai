import os

import pytest

from code_review_ai.installer import (
    DEFAULT_MCP_ENTRY, DEFAULT_NAME, DEFAULT_SOURCE,
    MCP_DOC_END, MCP_DOC_START, SKILL_NAMES,
    _claude_add_command, _claude_executable, _codex_add_command, _codex_executable,
    _global_context_file, _global_skills_dir,
    _launch_command, append_usage_docs, deploy_skills, install,
)


class _P:
    def __init__(self, returncode: int = 0, stderr: str = ""):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


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


def test_codex_add_command_shape():
    launch = ["uvx", "--from", "SRC", "code-review-ai-mcp"]
    assert _codex_add_command("code-review-ai", launch) == [
        "codex", "mcp", "add", "code-review-ai", "--",
        "uvx", "--from", "SRC", "code-review-ai-mcp",
    ]


def test_install_unsupported_platform():
    res = install(platform="cursor")
    assert res.success is False
    assert "unsupported" in res.message


def test_install_default_skips_global_mcp(monkeypatch, tmp_path):
    """Default install does NOT run `claude mcp add` — the review hook injects
    the server on-demand, so everyday sessions carry no tool-description cost."""
    monkeypatch.setattr("code_review_ai.installer.shutil.which",
                        lambda _: "/usr/bin/claude")
    monkeypatch.setattr("code_review_ai.installer.append_usage_docs",
                        lambda platform="claude-code": tmp_path / "CLAUDE.md")
    monkeypatch.setattr("code_review_ai.installer.deploy_skills",
                        lambda platform="claude-code", skills_root=None: tmp_path / "skills")
    calls = []
    monkeypatch.setattr("code_review_ai.installer.subprocess.run",
                        lambda cmd, **kw: calls.append(cmd) or _P(0))
    res = install()
    assert res.success is True
    assert calls == []  # no `claude mcp add`
    assert "NOT globally registered" in res.message
    assert "--register-mcp" in res.message


def test_install_register_mcp_runs_claude_mcp_add(monkeypatch, tmp_path):
    monkeypatch.setattr("code_review_ai.installer.shutil.which",
                        lambda _: "/usr/bin/claude")
    monkeypatch.setattr("code_review_ai.installer.append_usage_docs",
                        lambda platform="claude-code": tmp_path / "CLAUDE.md")
    monkeypatch.setattr("code_review_ai.installer.deploy_skills",
                        lambda platform="claude-code", skills_root=None: tmp_path / "skills")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _P(0)

    monkeypatch.setattr("code_review_ai.installer.subprocess.run", fake_run)
    res = install(register_mcp=True)
    assert res.success is True
    assert captured["cmd"] == [
        "/usr/bin/claude", "mcp", "add", DEFAULT_NAME, "-s", "user", "--",
        "uvx", "--from", DEFAULT_SOURCE, DEFAULT_MCP_ENTRY,
    ]
    assert "Appended tool usage docs" in res.message
    assert f"Deployed {len(SKILL_NAMES)} review skills" in res.message


def test_install_claude_not_found_when_registering(monkeypatch):
    monkeypatch.setattr("code_review_ai.installer.shutil.which", lambda _: None)
    res = install(register_mcp=True)
    assert res.success is False
    assert "claude CLI not found" in res.message
    # manual command is surfaced so the user can run it themselves
    assert "claude mcp add" in res.message
    assert "uvx" in res.command


def test_install_register_failure_does_not_append_docs(monkeypatch):
    monkeypatch.setattr("code_review_ai.installer.shutil.which",
                        lambda _: "/usr/bin/claude")
    calls = []

    class _P:
        returncode = 1
        stdout = ""
        stderr = "server already exists"

    monkeypatch.setattr("code_review_ai.installer.subprocess.run", lambda cmd, **kw: _P())
    monkeypatch.setattr("code_review_ai.installer.append_usage_docs",
                        lambda **kw: calls.append("docs") or None)
    monkeypatch.setattr("code_review_ai.installer.deploy_skills",
                        lambda **kw: calls.append("skills") or None)
    res = install(register_mcp=True)
    assert res.success is False
    assert "server already exists" in res.message
    assert calls == []  # 失败时不写文档、不部署 skill


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


def test_codex_executable_resolves_cmd_shim_on_windows(monkeypatch):
    monkeypatch.setattr("code_review_ai.installer.os.name", "nt")
    monkeypatch.setattr(
        "code_review_ai.installer.shutil.which",
        lambda name: {"codex": "C:/nvm/nodejs/codex",
                      "codex.cmd": "C:/nvm/nodejs/codex.cmd"}.get(name))
    assert _codex_executable() == "C:/nvm/nodejs/codex.cmd"


def test_append_usage_docs_is_idempotent(monkeypatch, tmp_path):
    md = tmp_path / "CLAUDE.md"
    monkeypatch.setattr("code_review_ai.installer._global_context_file",
                        lambda platform="claude-code": md)
    append_usage_docs()
    append_usage_docs()
    content = md.read_text(encoding="utf-8")
    assert content.count(MCP_DOC_START) == 1
    assert content.count(MCP_DOC_END) == 1
    assert "code-review-ai MCP tools" in content


def test_append_usage_docs_preserves_existing_content(monkeypatch, tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("<!-- CODEGRAPH_END -->\n", encoding="utf-8")
    monkeypatch.setattr("code_review_ai.installer._global_context_file",
                        lambda platform="claude-code": md)
    append_usage_docs()
    content = md.read_text(encoding="utf-8")
    assert content.startswith("<!-- CODEGRAPH_END -->")
    assert MCP_DOC_START in content


def test_append_usage_docs_codex_writes_agents_md(monkeypatch, tmp_path):
    agents = tmp_path / "AGENTS.md"
    monkeypatch.setattr("code_review_ai.installer._global_context_file",
                        lambda platform="codex": agents)
    append_usage_docs("codex")
    append_usage_docs("codex")
    content = agents.read_text(encoding="utf-8")
    assert content.count(MCP_DOC_START) == 1
    assert "code-review-ai MCP tools" in content


def test_deploy_skills_copies_all_skills_claude_code(monkeypatch, tmp_path):
    target = tmp_path / "claude-skills"
    monkeypatch.setattr("code_review_ai.installer._global_skills_dir",
                        lambda platform="claude-code": target)
    result = deploy_skills()
    assert result == target
    for name in SKILL_NAMES:
        text = (target / name / "SKILL.md").read_text(encoding="utf-8")
        assert f"name: {name}" in text


def test_deploy_skills_copies_all_skills_codex(monkeypatch, tmp_path):
    target = tmp_path / "codex-skills"
    monkeypatch.setattr("code_review_ai.installer._global_skills_dir",
                        lambda platform="codex": target)
    result = deploy_skills("codex")
    assert result == target
    for name in SKILL_NAMES:
        assert (target / name / "SKILL.md").exists()


def test_deploy_skills_is_idempotent(monkeypatch, tmp_path):
    target = tmp_path / "skills"
    monkeypatch.setattr("code_review_ai.installer._global_skills_dir",
                        lambda platform="claude-code": target)
    deploy_skills()
    before = {name: (target / name / "SKILL.md").read_text(encoding="utf-8")
              for name in SKILL_NAMES}
    deploy_skills()
    after = {name: (target / name / "SKILL.md").read_text(encoding="utf-8")
             for name in SKILL_NAMES}
    assert before == after
    assert len(list(target.iterdir())) == len(SKILL_NAMES)


def test_deploy_skills_missing_resource_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr("code_review_ai.installer.importlib.resources.files",
                        lambda *args, **kwargs: tmp_path / "does-not-exist")
    assert deploy_skills() is None


def test_install_codex_registers_mcp_and_deploys(monkeypatch, tmp_path):
    monkeypatch.setattr("code_review_ai.installer._codex_executable",
                        lambda: "/usr/bin/codex")
    monkeypatch.setattr("code_review_ai.installer.append_usage_docs",
                        lambda platform="codex": tmp_path / "AGENTS.md")
    monkeypatch.setattr("code_review_ai.installer.deploy_skills",
                        lambda platform="codex", skills_root=None: tmp_path / "codex-skills")

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _P()

    monkeypatch.setattr("code_review_ai.installer.subprocess.run", fake_run)
    res = install(platform="codex")
    assert res.success is True
    assert captured["cmd"] == [
        "/usr/bin/codex", "mcp", "add", DEFAULT_NAME, "--",
        "uvx", "--from", DEFAULT_SOURCE, DEFAULT_MCP_ENTRY,
    ]
    assert "Registered" in res.message
    assert f"Deployed {len(SKILL_NAMES)} review skills" in res.message


def test_install_codex_not_found(monkeypatch):
    monkeypatch.setattr("code_review_ai.installer._codex_executable", lambda: None)
    res = install(platform="codex")
    assert res.success is False
    assert "codex CLI not found" in res.message
    assert res.command[:4] == ["codex", "mcp", "add", DEFAULT_NAME]


@pytest.mark.parametrize("platform,suffix", [
    ("claude-code", ".claude/CLAUDE.md"),
    ("codex", ".codex/AGENTS.md"),
])
def test_global_context_file_mapping(monkeypatch, tmp_path, platform, suffix):
    monkeypatch.setattr("code_review_ai.installer.Path.home", lambda: tmp_path)
    assert _global_context_file(platform) == tmp_path / suffix


@pytest.mark.parametrize("platform,suffix", [
    ("claude-code", ".claude/skills"),
    ("codex", ".codex/skills"),
])
def test_global_skills_dir_mapping(monkeypatch, tmp_path, platform, suffix):
    monkeypatch.setattr("code_review_ai.installer.Path.home", lambda: tmp_path)
    assert _global_skills_dir(platform) == tmp_path / suffix
