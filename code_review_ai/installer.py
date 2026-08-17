"""Self-installation: register the MCP server with an AI tool's config and
deploy the bundled review skills + usage docs.

The subprocess / external-CLI coupling (``claude mcp add``) is isolated here so
``cli.py`` stays thin and the install logic is unit-testable without shelling out.
Codex has no ``mcp add`` CLI, so ``install --platform codex`` deploys skills and
usage docs only; MCP registration stays manual (see README).

The registered launch command is ``uvx --from <source> <mcp-entry>``: self-contained,
so an outsider needs only ``uv`` (which also fetches the required Python 3.14) and the
``claude`` CLI - no separate ``uv tool install`` step. On success the installer also
deploys the bundled language-review skills to the platform's user-scope skills dir
(``~/.claude/skills`` or ``~/.codex/skills``) and marker-injects a tool-usage section
into the platform's context file (``~/.claude/CLAUDE.md`` or ``~/.codex/AGENTS.md``,
marker-guarded, idempotent) so the AI in any project knows how to call the tools.
"""
import importlib.resources
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Where uvx fetches the package from. Channel A = public git repo.
DEFAULT_SOURCE = "git+https://github.com/Monkey-D-sj/code-review-ai"
DEFAULT_MCP_ENTRY = "code-review-ai-mcp"
DEFAULT_NAME = "code-review-ai"

SUPPORTED_PLATFORMS = {"claude-code", "codex"}

SKILL_NAMES = (
    "code-review-langs",
    "code-review-methodology",
    "code-review-python",
    "code-review-typescript",
    "code-review-javascript",
    "code-review-java",
)

# Tool-usage section marker-injected into the platform's context file on install
# (~/.claude/CLAUDE.md for claude-code, ~/.codex/AGENTS.md for codex). The block
# between the markers is replaced in place, so re-installs don't duplicate it.
MCP_DOC_START = "<!-- CODE_REVIEW_AI_MCP_START -->"
MCP_DOC_END = "<!-- CODE_REVIEW_AI_MCP_END -->"
MCP_USAGE_DOC = """\
<!-- CODE_REVIEW_AI_MCP_START -->
## code-review-ai MCP tools（user 作用域，任何项目可用）

These MCP tools are registered at user scope and query a tree-sitter + SQLite
call graph of whatever repo Claude Code is currently in. On first use in a new
project the server rebuilds that project's index automatically (a few seconds).

- `search_symbol(query)` — 先用它按短名 glob 找到 qname（如 `*login*`），再查细节/影响。
- `get_impact(symbols|files)` — **评估改动影响的首选**：传 `symbols`（如 `["auth::login"]`）或 `files`；都省略则从 git diff 推导。返回受影响入口 + 上下游调用链。别用 grep 硬猜。
- `get_symbol_detail(qname)` — 单个符号详情 + 直接 callers/callees。
- `list_entry_points()` — 看索引到的业务入口有哪些。
- `get_community(qname)` / `get_communities()` — 横向爆炸半径：符号所属社区及同社区成员，配合 `get_impact` 的纵向调用链互补。
- `rebuild_index()` — 手动刷新索引（正常由 watcher 自动维护，很少需要手动）。
- `call_external_service(body)` — 提交审查报告到外部服务（供 code-review skill 使用，一般不用直接调）。

约定：做"改了这个会影响什么"的分析时优先 `get_impact`；做"这些模块和谁耦合"的分析时优先社区工具。
<!-- CODE_REVIEW_AI_MCP_END -->
"""


@dataclass
class InstallResult:
    success: bool
    message: str
    command: list[str]


def _claude_executable() -> str | None:
    """The claude CLI as a full path subprocess can spawn, or None if missing.
    On Windows npm installs an extensionless shell script plus claude.cmd /
    claude.ps1 shims; the bare 'claude' name makes CreateProcess pick the
    extensionless script and fail (WinError 2), so resolve the .cmd shim."""
    path = shutil.which("claude")
    if path is None:
        return None
    if os.name == "nt":
        cmd = shutil.which("claude.cmd")
        if cmd:
            return cmd
    return path


def _global_context_file(platform: str) -> Path:
    """The platform's always-injected context file for tool-usage docs."""
    home = Path.home()
    if platform == "codex":
        return home / ".codex" / "AGENTS.md"
    return home / ".claude" / "CLAUDE.md"


def _global_skills_dir(platform: str) -> Path:
    """The platform's user-scope skills directory."""
    home = Path.home()
    if platform == "codex":
        return home / ".codex" / "skills"
    return home / ".claude" / "skills"


def append_usage_docs(platform: str = "claude-code") -> Path | None:
    """Append (or refresh) the MCP tool-usage section to the platform's
    user-global context file (CLAUDE.md / AGENTS.md), so the AI in any project
    knows how to call the tools. Idempotent: the block between the markers is
    replaced in place. Returns the path written, or None if it couldn't be
    written (install still succeeds)."""
    md = _global_context_file(platform)
    try:
        md.parent.mkdir(parents=True, exist_ok=True)
        content = md.read_text(encoding="utf-8") if md.exists() else ""
        start = content.find(MCP_DOC_START)
        end = content.find(MCP_DOC_END)
        if start != -1 and end != -1 and end > start:
            content = content[:start] + MCP_USAGE_DOC + content[end + len(MCP_DOC_END):]
        else:
            if content and not content.endswith("\n\n"):
                content = content.rstrip("\n") + "\n\n"
            content += MCP_USAGE_DOC
        md.write_text(content, encoding="utf-8")
        return md
    except OSError:
        return None


def deploy_skills(platform: str = "claude-code",
                  skills_root: Path | None = None) -> Path | None:
    """Copy the bundled language-review skills into the target platform's
    user-scope skills dir. Idempotent: overwrites SKILL.md in place. Returns
    the target dir, or None on failure (install still succeeds)."""
    target = skills_root or _global_skills_dir(platform)
    try:
        source = importlib.resources.files("code_review_ai").joinpath("skills")
        for name in SKILL_NAMES:
            skill_dir = target / name
            skill_dir.mkdir(parents=True, exist_ok=True)
            payload = (source / name / "SKILL.md").read_text(encoding="utf-8")
            (skill_dir / "SKILL.md").write_text(payload, encoding="utf-8")
        return target
    except (OSError, FileNotFoundError):
        return None


def _deploy_docs_and_skills(platform: str, msg: str) -> str:
    """Append usage docs + deploy skills for a platform; fold the outcomes
    into the success message. Both steps are non-fatal."""
    doc_path = append_usage_docs(platform)
    skills_dir = deploy_skills(platform)
    if doc_path is not None:
        msg += f" Appended tool usage docs to {doc_path}."
    if skills_dir is not None:
        msg += f" Deployed {len(SKILL_NAMES)} review skills to {skills_dir}."
    return msg


def _install_codex() -> InstallResult:
    """Codex has no ``codex mcp add`` CLI: deploy skills + usage docs only,
    MCP registration stays manual (edit ~/.codex/config.toml)."""
    msg = _deploy_docs_and_skills(
        "codex",
        "Registered review skills with Codex. MCP registration is manual: "
        "add a [mcp_servers.code-review-ai] block to ~/.codex/config.toml "
        "(see README).",
    )
    return InstallResult(True, msg, [])


def _install_claude(source: str, scope: str, name: str,
                    mcp_entry: str) -> InstallResult:
    """Register the MCP server with Claude Code, then deploy docs + skills."""
    add_cmd = _claude_add_command(name, scope, _launch_command(source, mcp_entry))
    claude = _claude_executable()
    if claude is None:
        return InstallResult(
            False,
            "claude CLI not found on PATH. Install Claude Code, then run:\n  "
            + " ".join(add_cmd),
            add_cmd,
        )
    add_cmd = [claude, *add_cmd[1:]]
    proc = subprocess.run(add_cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        return InstallResult(
            False,
            f"claude mcp add failed (exit {proc.returncode}):\n{detail}\n"
            f"If '{name}' already exists, remove it first: claude mcp remove {name}",
            add_cmd,
        )
    msg = _deploy_docs_and_skills(
        "claude-code",
        f"Registered '{name}' with Claude Code (scope={scope}).",
    )
    msg += " Restart Claude Code (or run /mcp) to see the tools."
    return InstallResult(True, msg, add_cmd)


def install(platform: str = "claude-code", source: str = DEFAULT_SOURCE,
            scope: str = "user", name: str = DEFAULT_NAME,
            mcp_entry: str = DEFAULT_MCP_ENTRY) -> InstallResult:
    """Register MCP + deploy skills/docs for the target platform. Returns a
    result; never raises - callers just print ``message`` and map ``success``
    to exit code."""
    if platform not in SUPPORTED_PLATFORMS:
        return InstallResult(False, f"unsupported platform: {platform}", [])
    if platform == "codex":
        return _install_codex()
    return _install_claude(source, scope, name, mcp_entry)


def _launch_command(source: str, mcp_entry: str) -> list[str]:
    """The command Claude Code will run to start the MCP server."""
    return ["uvx", "--from", source, mcp_entry]


def _claude_add_command(name: str, scope: str, launch_cmd: list[str]) -> list[str]:
    return ["claude", "mcp", "add", name, "-s", scope, "--", *launch_cmd]
