"""Self-installation: register the MCP server with an AI tool's config.

The subprocess / external-CLI coupling (``claude mcp add``) is isolated here so
``cli.py`` stays thin and the install logic is unit-testable without shelling out.

The registered launch command is ``uvx --from <source> <mcp-entry>``: self-contained,
so an outsider needs only ``uv`` (which also fetches the required Python 3.14) and the
``claude`` CLI - no separate ``uv tool install`` step.
"""
import shutil
import subprocess
from dataclasses import dataclass

# Where uvx fetches the package from. Channel A = public git repo.
DEFAULT_SOURCE = "git+https://github.com/Monkey-D-sj/code-review-ai"
DEFAULT_MCP_ENTRY = "code-review-ai-mcp"
DEFAULT_NAME = "code-review-ai"

SUPPORTED_PLATFORMS = {"claude-code"}


@dataclass
class InstallResult:
    success: bool
    message: str
    command: list[str]


def install(platform: str = "claude-code", source: str = DEFAULT_SOURCE,
            scope: str = "user", name: str = DEFAULT_NAME,
            mcp_entry: str = DEFAULT_MCP_ENTRY) -> InstallResult:
    """Register the MCP server with the target platform. Returns a result;
    never raises - callers just print ``message`` and map ``success`` to exit code."""
    if platform not in SUPPORTED_PLATFORMS:
        return InstallResult(False, f"unsupported platform: {platform}", [])
    launch_cmd = _launch_command(source, mcp_entry)
    add_cmd = _claude_add_command(name, scope, launch_cmd)
    if shutil.which("claude") is None:
        return InstallResult(
            False,
            "claude CLI not found on PATH. Install Claude Code, then run:\n  "
            + " ".join(add_cmd),
            add_cmd,
        )
    proc = subprocess.run(add_cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return InstallResult(
            True,
            f"Registered '{name}' with Claude Code (scope={scope}). "
            "Restart Claude Code (or run /mcp) to see the tools.",
            add_cmd,
        )
    detail = (proc.stderr or proc.stdout).strip()
    return InstallResult(
        False,
        f"claude mcp add failed (exit {proc.returncode}):\n{detail}\n"
        f"If '{name}' already exists, remove it first: claude mcp remove {name}",
        add_cmd,
    )


def _launch_command(source: str, mcp_entry: str) -> list[str]:
    """The command Claude Code will run to start the MCP server."""
    return ["uvx", "--from", source, mcp_entry]


def _claude_add_command(name: str, scope: str, launch_cmd: list[str]) -> list[str]:
    return ["claude", "mcp", "add", name, "-s", scope, "--", *launch_cmd]
