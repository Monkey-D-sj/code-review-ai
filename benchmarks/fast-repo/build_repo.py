"""Build the synthetic ``fast-repo`` git history for the full-agent eval.

The eval's reverse-mutation machinery clones a repo, checks out a worktree at
``source_commit``, then restores the parent commit's version of each mutated
path so the agent reviews an unstaged diff. This script materializes the
deterministic git history that makes that work for a small committed seed:

- ``src/`` holds the *fixed* versions of every module.
- Each ``cases/<slug>/mutation.diff`` is a FIXED -> BUGGY unified diff for one
  module (the change the agent is asked to review).
- For each case we commit ``buggy-<slug>`` (mutation applied) then
  ``fix-<slug>`` (mutation reverted), and tag a ``fix-<slug>`` branch. The
  eval checks out ``fix-<slug>`` and restores ``fix-<slug>^`` (the buggy
  module), so the agent sees exactly the mutation diff. Every non-mutated
  module stays fixed in every commit, so cases are isolated.

The built repo lives at ``--target`` (the gitignored eval cache, e.g.
``.code-review-ai/external-repos/fast-repo``), never inside the seed, so the
parent ``code-review-ai`` repo never sees a nested ``.git``.

Idempotent: a marker hash over the seed sources, the mutation diffs, and this
script's revision decides whether ``--target`` is up to date; ``--force``
rebuilds unconditionally.

Usage::

    python benchmarks/fast-repo/build_repo.py \
        --seed benchmarks/fast-repo --target .code-review-ai/external-repos/fast-repo
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

VERSION = 1


def _rmtree_force(path: Path) -> None:
    """Remove a directory even when git left objects with the read-only bit.

    On Windows, git marks loose objects read-only and ``shutil.rmtree`` then
    fails with PermissionError; clear the bit and retry.
    """
    def _handle(func, item, exc_info):
        try:
            os.chmod(item, 0o777)
        except OSError:
            pass
        func(item)

    shutil.rmtree(path, onexc=_handle)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   text=True, encoding="utf-8", errors="replace")


def file_bytes(path: Path) -> bytes:
    # The parent repo checks files out as CRLF on Windows (core.autocrlf), so
    # normalize to LF here and pin autocrlf off in the built repo — otherwise
    # the LF mutation diffs would not apply.
    return path.read_bytes().replace(b"\r\n", b"\n")


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(b"\0")
            digest.update(file_bytes(path))
            digest.update(b"\0")
    return digest.hexdigest()


def marker(seed: Path) -> str:
    payload = f"v{VERSION}|{tree_digest(seed / 'src')}|{tree_digest(seed / 'cases')}"
    return hashlib.sha256(payload.encode()).hexdigest()


def apply_mutation(target: Path, diff_text: str, reverse: bool) -> None:
    args = ["git", "apply"]
    if reverse:
        args.append("-R")
    args += ["--whitespace=nowarn", "-"]
    # Send the patch as bytes (binary stdin): text mode would translate LF to
    # CRLF on Windows and the LF-only tracked files would never match.
    subprocess.run(args, cwd=target, input=diff_text.encode("utf-8"),
                   check=True, capture_output=True)
    changed = subprocess.run(["git", "diff", "--quiet"], cwd=target,
                             capture_output=True)
    if changed.returncode == 0:
        raise ValueError(f"mutation.diff applied as a no-op in {target}")


def build(seed: Path, target: Path, force: bool) -> None:
    expected = marker(seed)
    marker_path = target / ".fast-repo-marker"
    if not force and (target / ".git").exists() and marker_path.exists() \
            and marker_path.read_text(encoding="utf-8").strip() == expected:
        print(f"fast-repo up to date at {target}")
        return
    if target.exists():
        _rmtree_force(target)
    target.mkdir(parents=True)
    for source in (seed / "src").rglob("*"):
        if source.is_file():
            dest = target / "src" / source.relative_to(seed / "src")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(file_bytes(source))
    git(target, "init", "-q", "-b", "main")
    git(target, "config", "core.autocrlf", "false")
    git(target, "config", "user.name", "fast-repo")
    git(target, "config", "user.email", "fast-repo@localhost")
    git(target, "add", "-A")
    git(target, "commit", "-q", "-m", "base: fixed store")
    case_count = 0
    for diff_path in sorted((seed / "cases").glob("*/mutation.diff")):
        case_id = diff_path.parent.name
        diff_text = file_bytes(diff_path).decode("utf-8")
        apply_mutation(target, diff_text, reverse=False)
        git(target, "add", "-A")
        git(target, "commit", "-q", "-m", f"buggy-{case_id}")
        git(target, "branch", f"buggy-{case_id}")
        apply_mutation(target, diff_text, reverse=True)
        git(target, "add", "-A")
        git(target, "commit", "-q", "-m", f"fix-{case_id}")
        git(target, "branch", f"fix-{case_id}")
        case_count += 1
    marker_path.write_text(expected, encoding="utf-8")
    print(f"built fast-repo ({case_count} cases) at {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", required=True, type=Path,
                        help="committed seed dir (benchmarks/fast-repo)")
    parser.add_argument("--target", required=True, type=Path,
                        help="where to materialize the git repo")
    parser.add_argument("--force", action="store_true",
                        help="rebuild even if the marker says up to date")
    args = parser.parse_args(argv)
    seed = args.seed.resolve()
    if not (seed / "src").is_dir():
        print(f"error: --seed has no src/ tree: {seed}", file=sys.stderr)
        return 1
    build(seed, args.target.resolve(), args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
