from conftest import Q
from code_review_ai.config import Config, load_config


def test_load_config_defaults(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.code-review-ai]\nrepo_path = "."\n', encoding="utf-8"
    )
    cfg = load_config(str(tmp_path))
    assert isinstance(cfg, Config)
    assert cfg.repo_path == "."
    assert cfg.diff_base == "origin/main"        # default
    assert cfg.max_depth == 10                    # default
    assert cfg.entry_names == ["main"]            # default heuristic
