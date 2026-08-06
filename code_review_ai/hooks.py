"""Install post-* git hooks that keep flows/communities fresh at commit time.

The hooks call `code-review-ai sync` (nodes catch-up + flows + communities) so
the index reflects the last commit exactly. Per-repo setup; repos without hooks
still self-heal at startup via the flows_as_of_head check.

The hook self-bootstraps: at runtime it prefers a PATH-installed `code-review-ai`
and otherwise falls back to `uvx --from <source>`, so no global install is
required. A custom `launch` is used verbatim instead.

Hooks are written to the directory git actually reads: `core.hooksPath` if set,
else `.git/hooks`. Husky's hooksPath is its auto-generated `.husky/_` shim dir,
so for husky the hooks go to `.husky/` (the files the shims source).

With `with_review`, the post-commit hook additionally summarizes the commit's
change impact and hands it to an LLM (``claude -p`` by default), writing the
report to ``.code-review-ai/last-review.md``. Review runs on post-commit only,
where the changed-file set is unambiguous; the other hooks always sync only.
"""

import re
import subprocess
from pathlib import Path

from code_review_ai.installer import DEFAULT_SOURCE

HOOK_NAMES = ("post-commit", "post-merge", "post-checkout", "post-rewrite")

_SOURCE_SUFFIX_RE = re.compile(r"\.(py|ts|tsx|js|mjs|cjs|jsx|vue)$", re.IGNORECASE)
_REVIEW_PROMPT = (
    "对以下代码变更影响做代码评审：按 error / warning / info 三级输出发现，"
    "每条给出文件、行号、问题描述与具体失败场景，用中文回答。"
)


def install_hooks(repo: str, db: str, launch: str = "code-review-ai",
                  with_review: bool = False,
                  review_launch: str = "claude -p",
                  review_out: str | None = None,
                  source: str = DEFAULT_SOURCE) -> list[str]:
    """Write the post-* hooks under the dir git actually reads: `core.hooksPath`
    if set, else <repo>/.git/hooks. Husky's hooksPath points at its auto-generated
    `.husky/_` shim dir, so for husky the hooks land in `.husky/` where the shims
    source them from. Returns paths."""
    hooks_dir = _resolve_hooks_dir(repo)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name in HOOK_NAMES:
        review = name == "post-commit" and with_review
        script = (_review_script(repo, db, launch, review_launch, review_out, source)
                  if review else _sync_script(repo, db, launch, source))
        path = hooks_dir / name
        path.write_text(script, encoding="utf-8")
        _make_executable(path)
        written.append(str(path))
    return written


def _resolve_hooks_dir(repo: str) -> Path:
    """Where git runs hooks from. Returns the husky user-hook dir when
    `core.hooksPath` points at `.husky/_`; otherwise the hooksPath dir itself,
    or `.git/hooks` when hooksPath is unset."""
    default = Path(repo) / ".git" / "hooks"
    hooks_path = _read_hooks_path(repo)
    if hooks_path is None:
        return default
    path = Path(hooks_path)
    if not path.is_absolute():
        path = Path(repo) / path
    if path.name == "_" and path.parent.name == ".husky":
        return path.parent
    return path


def _read_hooks_path(repo: str) -> str | None:
    """`git config core.hooksPath` resolved relative to repo root, or None."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "config", "--path", "--get", "core.hooksPath"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _make_executable(path: Path) -> None:
    try:
        path.chmod(0o755)
    except OSError:
        pass


def _launcher_line(launch: str, source: str) -> str:
    """Resolve the code-review-ai command at hook runtime. The default launch
    prefers a PATH install and falls back to `uvx --from <source>` so the hook
    works without a global install; a custom launch is used verbatim."""
    if launch != "code-review-ai":
        return f"LAUNCH='{launch}'\n"
    return (
        "if command -v code-review-ai >/dev/null 2>&1; then\n"
        "  LAUNCH='code-review-ai'\n"
        "else\n"
        f"  LAUNCH='uvx --from {source} code-review-ai'\n"
        "fi\n"
    )


def _sync_script(repo: str, db: str, launch: str, source: str) -> str:
    repo_abs = str(Path(repo).resolve())
    db_abs = str(Path(db).resolve()).replace("\\", "/")
    return (
        "#!/bin/sh\n"
        "# code-review-ai: rebuild flows/communities at commit time\n"
        + _launcher_line(launch, source)
        + f"$LAUNCH sync --repo '{repo_abs}' --db '{db_abs}'\n"
    )


def _review_script(repo: str, db: str, launch: str, review_launch: str,
                   review_out: str | None, source: str) -> str:
    repo_abs = str(Path(repo).resolve())
    db_abs = str(Path(db).resolve()).replace("\\", "/")
    out_abs = str(Path(review_out or Path(repo_abs) / ".code-review-ai" / "last-review.md")
                  .resolve()).replace("\\", "/")
    return (
        "#!/bin/sh\n"
        "# code-review-ai: rebuild index + review the commit's change impact\n"
        + _launcher_line(launch, source)
        + f"$LAUNCH sync --repo '{repo_abs}' --db '{db_abs}' >/dev/null 2>&1\n"
        + "files=$(git diff-tree --name-only --no-commit-id HEAD -r 2>/dev/null "
        f"| grep -E '{_SOURCE_SUFFIX_RE.pattern}' || true)\n"
        + '# root commit has no parent: nothing to diff against, skip\n'
        + '[ -z "$files" ] && exit 0\n'
        + "# CRAI_DIFF_BASE=HEAD^ diffs the commit itself, so the hook works\n"
        + "# even before origin/main exists\n"
        + f"summary=$(CRAI_DIFF_BASE=HEAD^ $LAUNCH summary --repo '{repo_abs}' "
        f"--db '{db_abs}' --files $files 2>/dev/null) || exit 0\n"
        + f"printf '%s' \"$summary\" | {review_launch} "
        f"'{_REVIEW_PROMPT}' --output-format text > '{out_abs}' 2>/dev/null || exit 0\n"
        + f"echo \"code-review-ai: review written to {out_abs}\"\n"
    )
