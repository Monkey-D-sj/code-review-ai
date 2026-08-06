"""Install post-* git hooks that keep flows/communities fresh at commit time.

The hooks call `code-review-ai sync` (nodes catch-up + flows + communities) so
the index reflects the last commit exactly. Per-repo setup; repos without hooks
still self-heal at startup via the flows_as_of_head check.

With `with_review`, the post-commit hook additionally summarizes the commit's
change impact and hands it to an LLM (``claude -p`` by default), writing the
report to ``.code-review-ai/last-review.md``. Review runs on post-commit only,
where the changed-file set is unambiguous; the other hooks always sync only.
"""

import re
from pathlib import Path

HOOK_NAMES = ("post-commit", "post-merge", "post-checkout", "post-rewrite")

_SOURCE_SUFFIX_RE = re.compile(r"\.(py|ts|tsx|js|mjs|cjs|jsx|vue)$", re.IGNORECASE)
_REVIEW_PROMPT = (
    "对以下代码变更影响做代码评审：按 error / warning / info 三级输出发现，"
    "每条给出文件、行号、问题描述与具体失败场景，用中文回答。"
)


def install_hooks(repo: str, db: str, launch: str = "code-review-ai",
                  with_review: bool = False,
                  review_launch: str = "claude -p",
                  review_out: str | None = None) -> list[str]:
    """Write the post-* hooks under <repo>/.git/hooks. Returns paths."""
    hooks_dir = _ensure_hooks_dir(repo)
    written: list[str] = []
    for name in HOOK_NAMES:
        review = name == "post-commit" and with_review
        script = (_review_script(repo, db, launch, review_launch, review_out)
                  if review else _sync_script(repo, db, launch))
        path = hooks_dir / name
        path.write_text(script, encoding="utf-8")
        _make_executable(path)
        written.append(str(path))
    return written


def _ensure_hooks_dir(repo: str) -> Path:
    hooks_dir = Path(repo) / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    return hooks_dir


def _make_executable(path: Path) -> None:
    try:
        path.chmod(0o755)
    except OSError:
        pass


def _sync_script(repo: str, db: str, launch: str) -> str:
    repo_abs = str(Path(repo).resolve())
    db_abs = str(Path(db).resolve()).replace("\\", "/")
    return (
        "#!/bin/sh\n"
        "# code-review-ai: rebuild flows/communities at commit time\n"
        f"{launch} sync --repo '{repo_abs}' --db '{db_abs}'\n"
    )


def _review_script(repo: str, db: str, launch: str, review_launch: str,
                   review_out: str | None) -> str:
    repo_abs = str(Path(repo).resolve())
    db_abs = str(Path(db).resolve()).replace("\\", "/")
    out_abs = str(Path(review_out or Path(repo_abs) / ".code-review-ai" / "last-review.md")
                  .resolve()).replace("\\", "/")
    return (
        "#!/bin/sh\n"
        "# code-review-ai: rebuild index + review the commit's change impact\n"
        f"{launch} sync --repo '{repo_abs}' --db '{db_abs}' >/dev/null 2>&1\n"
        "files=$(git diff-tree --name-only --no-commit-id HEAD -r 2>/dev/null "
        f"| grep -E '{_SOURCE_SUFFIX_RE.pattern}' || true)\n"
        '[ -z "$files" ] && exit 0\n'
        f"summary=$({launch} summary --repo '{repo_abs}' --db '{db_abs}' "
        "--files $files 2>/dev/null) || exit 0\n"
        f"printf '%s' \"$summary\" | {review_launch} "
        f"'{_REVIEW_PROMPT}' --output-format text > '{out_abs}' 2>/dev/null || exit 0\n"
        f"echo \"code-review-ai: review written to {out_abs}\"\n"
    )
