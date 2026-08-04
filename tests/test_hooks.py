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
