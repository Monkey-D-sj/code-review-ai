import subprocess
from pathlib import Path
from code_review_ai.hooks import HOOK_NAMES, install_hooks


def test_install_hooks_writes_sync_scripts(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    written = install_hooks(str(repo), str(tmp_path / "index.db"), launch="code-review-ai")
    assert len(written) == len(HOOK_NAMES)
    for name in HOOK_NAMES:
        p = Path(written[HOOK_NAMES.index(name)])
        content = p.read_text(encoding="utf-8")
        assert "sync --repo" in content
        assert "--db" in content


def test_install_hooks_idempotent(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    first = install_hooks(str(repo), str(tmp_path / "i.db"))
    second = install_hooks(str(repo), str(tmp_path / "i.db"))
    assert first == second
    assert Path(first[0]).read_text(encoding="utf-8") == Path(second[0]).read_text(encoding="utf-8")


def test_install_hooks_with_review_writes_review_only_on_post_commit(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    written = install_hooks(str(repo), str(tmp_path / "i.db"),
                            launch="code-review-ai", with_review=True)
    post_commit = Path(written[HOOK_NAMES.index("post-commit")]).read_text(encoding="utf-8")
    post_merge = Path(written[HOOK_NAMES.index("post-merge")]).read_text(encoding="utf-8")
    assert "--files" in post_commit
    assert "CRAI_DIFF_BASE=HEAD^" in post_commit
    assert "claude -p" in post_commit
    assert "last-review.md" in post_commit
    assert "--files" not in post_merge


def test_install_hooks_review_honors_custom_launch_and_out(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    review_out = tmp_path / "review.md"
    written = install_hooks(str(repo), str(tmp_path / "i.db"), launch="my-cr",
                            with_review=True, review_launch="codex exec",
                            review_out=str(review_out))
    content = Path(written[HOOK_NAMES.index("post-commit")]).read_text(encoding="utf-8")
    assert "LAUNCH='my-cr'" in content
    assert "codex exec" in content
    assert "review.md" in content
    assert "claude -p" not in content
    assert "uvx --from" not in content  # custom launch bypasses the uvx fallback


def test_install_hooks_default_launch_falls_back_to_uvx(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    written = install_hooks(str(repo), str(tmp_path / "i.db"))
    content = Path(written[0]).read_text(encoding="utf-8")
    assert "command -v code-review-ai" in content
    assert "LAUNCH='uvx --from" in content
    assert "$LAUNCH sync" in content


def _init_git(repo):
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)


def test_install_hooks_writes_to_husky_dir(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _init_git(repo)
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", ".husky/_"],
                   check=True)
    written = install_hooks(str(repo), str(tmp_path / "i.db"))
    assert written[0] == str(repo / ".husky" / "post-commit")
    assert (repo / ".husky" / "post-commit").exists()
    assert not (repo / ".git" / "hooks" / "post-commit").exists()


def test_install_hooks_honors_custom_hooks_path(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _init_git(repo)
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", "custom-hooks"],
                   check=True)
    written = install_hooks(str(repo), str(tmp_path / "i.db"))
    assert written[0] == str(repo / "custom-hooks" / "post-commit")
    assert (repo / "custom-hooks" / "post-commit").exists()
