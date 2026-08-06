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
    "对以下代码变更影响做代码评审。输入是 code-review-ai 生成的变更摘要 JSON。"
    "请按顺序使用 code-review-ai 的 MCP 工具：先用 get_change_summary 确认变更明细，"
    "再用 get_impact 查上游调用方 / 下游被调方 / 受影响业务入口，"
    "search_symbol / query_graph 按需补充；不要用 git diff / grep 自己重算；"
    "再按语言用 code-review 系列 skill 评审，只在必要时用 Read 确认文件细节。"
    "按 error / warning / info 三级输出发现，每条给出文件、行号、问题描述与具体失败场景，用中文回答。"
)
# Per-platform review LLM invocation: (launch command, args, answer mode).
# Answer mode "extract" parses the final answer out of a stream-json debug log
# via `code-review-ai extract-review`; "stdout" takes the review directly from
# stdout. claude -p only exposes tool/skill/MCP activity through
# --output-format stream-json (which --verbose is required alongside in --print
# mode); codex exec prints the answer on stdout and already streams activity to
# stderr, so it needs no extraction.
# --allowedTools pre-authorizes the review's tools so a headless `claude -p`
# run can use them without an interactive permission prompt (MCP tools prompt by
# default and fail in non-interactive hooks); codex needs --full-auto to proceed
# without approvals.
_PLATFORM_REVIEW: dict[str, tuple[str, str, str]] = {
    "claude-code": (
        "claude -p",
        '--output-format stream-json --verbose '
        '--allowedTools Bash Read Grep Glob "mcp__code-review-ai__*" "mcp__codegraph__*"',
        "extract",
    ),
    "codex": ("codex exec", "--full-auto", "stdout"),
}


def install_hooks(repo: str, db: str, launch: str = "code-review-ai",
                  with_review: bool = False,
                  platform: str = "claude-code",
                  review_launch: str | None = None,
                  review_out: str | None = None,
                  source: str = DEFAULT_SOURCE) -> list[str]:
    """Write the post-* hooks under the dir git actually reads: `core.hooksPath`
    if set, else <repo>/.git/hooks. Husky's hooksPath points at its auto-generated
    `.husky/_` shim dir, so for husky the hooks land in `.husky/` where the shims
    source them from. Returns paths.

    `platform` selects the review LLM's default command (`claude-code` ->
    `claude -p --output-format text`, `codex` -> `codex exec`). An explicit
    `review_launch` overrides the platform's command entirely."""
    if platform not in _PLATFORM_REVIEW:
        raise ValueError(f"unsupported review platform: {platform}")
    review_cmd, review_args, answer_mode = _PLATFORM_REVIEW[platform]
    if review_launch is not None:
        review_cmd, review_args, answer_mode = review_launch, "", "stdout"
    hooks_dir = _resolve_hooks_dir(repo)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name in HOOK_NAMES:
        review = name == "post-commit" and with_review
        script = (_review_script(repo, db, launch, review_cmd, review_args,
                                 answer_mode, review_out, source)
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
        "set +e\n"
        + _launcher_line(launch, source)
        + 'echo "code-review-ai: syncing code graph index..."\n'
        + f"$LAUNCH sync --repo '{repo_abs}' --db '{db_abs}'\n"
        + "exit 0\n"
    )


def _review_script(repo: str, db: str, launch: str, review_cmd: str,
                   review_args: str, answer_mode: str, review_out: str | None,
                   source: str) -> str:
    repo_abs = str(Path(repo).resolve())
    db_abs = str(Path(db).resolve()).replace("\\", "/")
    out_abs = str(Path(review_out or Path(repo_abs) / ".code-review-ai" / "last-review.md")
                  .resolve()).replace("\\", "/")
    review_block = (_extract_review_block(review_cmd, review_args)
                    if answer_mode == "extract"
                    else _direct_review_block(review_cmd, review_args))
    return (
        "#!/bin/sh\n"
        "# code-review-ai: rebuild index + review the commit's change impact\n"
        "# set +e: never let errexit abort the hook; failures are logged, not fatal\n"
        "set +e\n"
        + _launcher_line(launch, source)
        + 'echo "code-review-ai: syncing code graph index..."\n'
        + f"if ! $LAUNCH sync --repo '{repo_abs}' --db '{db_abs}'; then\n"
        + '  echo "code-review-ai: sync failed; skipping review (sh -x <hook> to debug)" >&2\n'
        + "  exit 0\n"
        + "fi\n"
        + "files=$(git diff-tree --name-only --no-commit-id HEAD -r 2>/dev/null "
        f"| grep -E '{_SOURCE_SUFFIX_RE.pattern}' || true)\n"
        + 'if [ -z "$files" ]; then\n'
        + '  echo "code-review-ai: no source files changed; skipping review"\n'
        + "  exit 0\n"
        + "fi\n"
        + 'echo "code-review-ai: changed files: $files"\n'
        + 'echo "code-review-ai: building change summary..."\n'
        + "# CRAI_DIFF_BASE=HEAD^ diffs the commit itself, so the hook works\n"
        + "# even before origin/main exists\n"
        + f"summary=$(CRAI_DIFF_BASE=HEAD^ $LAUNCH summary --repo '{repo_abs}' "
        f"--db '{db_abs}' --files $files)\n"
        + 'if [ -z "$summary" ]; then\n'
        + '  echo "code-review-ai: change summary failed; skipping review" >&2\n'
        + "  exit 0\n"
        + "fi\n"
        + f'echo "code-review-ai: reviewing with {review_cmd}..."\n'
        + "# archive one copy per commit under reviews/<date>/<stamp>-<short-sha>.md;\n"
        + "# <out> stays a latest pointer for convenience\n"
        + "date_dir=$(date +%F)\n"
        + "stamp=$(date +%F-%H%M%S)\n"
        + "short_sha=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)\n"
        + f"archive_dir='{repo_abs}/.code-review-ai/reviews/'\"$date_dir\"\n"
        + 'mkdir -p "$archive_dir"\n'
        + 'archive="$archive_dir/${stamp}-${short_sha}.md"\n'
        + 'debug="${archive}.debug.log"\n'
        + 'raw="${archive}.debug.jsonl"\n'
        + review_block
        + f"  cp \"$archive\" '{out_abs}'\n"
        + (f"  cp \"$raw\" '{out_abs}.debug.jsonl'\n" if answer_mode == "extract" else "")
        + f"  cp \"$debug\" '{out_abs}.debug.log'\n"
        + '  echo "code-review-ai: review written to $archive (debug: $debug)"\n'
        + "exit 0\n"
    )


def _extract_review_block(review_cmd: str, review_args: str) -> str:
    """claude: capture the full stream-json flow to $raw, extract the final
    answer into $archive via `code-review-ai extract-review`, and distill a
    concise per-tool trace into $debug via `code-review-ai trace-review`."""
    return (
        f"printf '%s' \"$summary\" | {review_cmd} "
        f"'{_REVIEW_PROMPT}' {review_args} > \"$raw\" 2>/dev/null\n"
        + "review_status=$?\n"
        + 'if [ "$review_status" -eq 0 ]; then\n'
        + "  $LAUNCH extract-review \"$raw\" \"$archive\"\n"
        + "  extract_status=$?\n"
        + '  if [ "$extract_status" -ne 0 ] || [ ! -s "$archive" ]; then\n'
        + '    echo "code-review-ai: review produced no answer text (debug: $raw)" >&2\n'
        + "    exit 0\n"
        + "  fi\n"
        + "  $LAUNCH trace-review \"$raw\" \"$debug\"\n"
        + "else\n"
        + '  echo "code-review-ai: review command failed (debug: $raw)" >&2\n'
        + "  exit 0\n"
        + "fi\n"
    )


def _direct_review_block(review_cmd: str, review_args: str) -> str:
    """codex / custom launch: stdout is the answer, stderr the flow."""
    return (
        f"printf '%s' \"$summary\" | {review_cmd} "
        f"'{_REVIEW_PROMPT}' {review_args} > \"$archive\" 2> \"$debug\"\n"
        + "review_status=$?\n"
        + 'if [ "$review_status" -ne 0 ]; then\n'
        + '  echo "code-review-ai: review command failed (debug: $debug)" >&2\n'
        + "  exit 0\n"
        + "fi\n"
    )
