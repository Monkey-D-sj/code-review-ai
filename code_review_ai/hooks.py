"""Install post-* git hooks that keep flows/communities fresh at commit time.

The hooks call `code-review-ai sync` (nodes catch-up + flows + communities) so
the index reflects the last commit exactly. Per-repo setup; repos without hooks
still self-heal at startup via the flows_as_of_head check."""

from pathlib import Path

HOOK_NAMES = ("post-commit", "post-merge", "post-checkout", "post-rewrite")


def install_hooks(repo: str, db: str, launch: str = "code-review-ai") -> list[str]:
    """Write the four post-* hooks under <repo>/.git/hooks. Returns paths."""
    repo_abs = str(Path(repo).resolve())
    db_abs = str(Path(db).resolve()).replace("\\", "/")
    hooks_dir = Path(repo) / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    script = (
        "#!/bin/sh\n"
        "# code-review-ai: rebuild flows/communities at commit time\n"
        f"{launch} sync --repo '{repo_abs}' --db '{db_abs}'\n"
    )
    written: list[str] = []
    for name in HOOK_NAMES:
        path = hooks_dir / name
        path.write_text(script, encoding="utf-8")
        try:
            path.chmod(0o755)
        except OSError:
            pass
        written.append(str(path))
    return written
