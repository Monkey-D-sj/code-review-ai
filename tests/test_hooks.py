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


def test_install_hooks_codex_platform_uses_codex_exec(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    written = install_hooks(str(repo), str(tmp_path / "i.db"), with_review=True,
                            platform="codex")
    content = Path(written[HOOK_NAMES.index("post-commit")]).read_text(encoding="utf-8")
    assert "codex exec" in content
    assert "claude -p" not in content
    assert "--output-format text" not in content
    assert ".debug.log" in content  # codex streams activity to stderr by default


def test_install_hooks_review_captures_debug_log(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    written = install_hooks(str(repo), str(tmp_path / "i.db"), with_review=True)
    content = Path(written[HOOK_NAMES.index("post-commit")]).read_text(encoding="utf-8")
    assert "--output-format stream-json --verbose" in content
    assert "--allowedTools" in content
    assert 'mcp__code-review-ai__*' in content
    assert "extract-review" in content
    assert ".debug.log" in content


def test_install_hooks_review_prompt_orders_change_summary_first(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    written = install_hooks(str(repo), str(tmp_path / "i.db"), with_review=True)
    content = Path(written[HOOK_NAMES.index("post-commit")]).read_text(encoding="utf-8")
    assert content.index("get_change_summary") < content.index("get_impact")


def test_install_hooks_review_archives_by_date(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    written = install_hooks(str(repo), str(tmp_path / "i.db"), with_review=True)
    content = Path(written[HOOK_NAMES.index("post-commit")]).read_text(encoding="utf-8")
    assert "date +%F" in content
    assert "reviews/" in content
    assert "mkdir -p" in content
    assert 'cp "$archive"' in content
    assert '${stamp}-${short_sha}.md' in content


def test_install_hooks_review_launch_overrides_platform(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    written = install_hooks(str(repo), str(tmp_path / "i.db"), with_review=True,
                            platform="codex", review_launch="claude -p")
    content = Path(written[HOOK_NAMES.index("post-commit")]).read_text(encoding="utf-8")
    assert "claude -p" in content
    assert "--output-format text" not in content  # explicit launch = full control


def test_install_hooks_unsupported_platform_raises(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    try:
        install_hooks(str(repo), str(tmp_path / "i.db"), with_review=True,
                      platform="copilot")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unsupported platform")


def test_install_hooks_review_has_progress_output(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    written = install_hooks(str(repo), str(tmp_path / "i.db"), with_review=True)
    content = Path(written[HOOK_NAMES.index("post-commit")]).read_text(encoding="utf-8")
    assert "set +e" in content
    assert "syncing code graph index" in content
    assert "changed files:" in content
    assert "change summary failed" in content
    assert "review written" in content


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


def test_install_hooks_review_prompt_contains_context_depth_routing(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    written = install_hooks(str(repo), str(tmp_path / "i.db"), with_review=True)
    content = Path(written[HOOK_NAMES.index("post-commit")]).read_text(encoding="utf-8")
    assert "自包含" in content            # 重要性判据
    assert "query_graph" in content       # 默认动作
    assert "direction=in" in content      # 默认看上游
    assert "跨服务" in content            # 深度路由:跨服务才 get_impact
    assert content.index("get_change_summary") < content.index("get_impact")
