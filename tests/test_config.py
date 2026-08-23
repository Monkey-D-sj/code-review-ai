import json
from dataclasses import replace

from code_review_ai.config import Config, _jsonc_clean, config_hash, load_config


def test_load_config_defaults(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.code-review-ai]\nrepo_path = "."\n', encoding="utf-8"
    )
    cfg = load_config(str(tmp_path))
    assert isinstance(cfg, Config)
    assert cfg.repo_path == "."
    assert cfg.diff_base == "origin/main"        # default
    assert cfg.entry_names == ["main"]            # default heuristic
    assert cfg.dependency_markers == ["Depends", "Security"]  # FastAPI DI markers
    assert cfg.path_aliases == {}                 # default: no aliases


def test_path_aliases_detected_from_tsconfig(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.code-review-ai]\nrepo_path = "{tmp_path.as_posix()}"\n', encoding="utf-8"
    )
    # Real tsconfig.json is JSONC: comments + trailing commas.
    (tmp_path / "tsconfig.json").write_text(
        "{\n"
        '  "compilerOptions": {\n'
        '    "baseUrl": ".",\n'
        '    // path aliases\n'
        '    "paths": {\n'
        '      "@/*": ["src/*"],\n'
        '      "@lib/*": ["./lib/*"], // trailing comment\n'
        "    },\n"
        "  },\n"
        "}\n",
        encoding="utf-8",
    )
    cfg = load_config(str(tmp_path))
    # "@/*" -> prefix "@/", target "src/" ; "@lib/*" -> "@lib/", "./lib/*" -> "lib/"
    assert cfg.path_aliases == {"@/": "src/", "@lib/": "lib/"}


def test_jsonc_clean_leaves_strings_untouched():
    src = '{\n  "url": "http://a/b//c",\n  // comment\n  "x": [1, 2,],\n}'
    cleaned = _jsonc_clean(src)
    assert "http://a/b//c" in cleaned          # // inside a string survives
    assert "// comment" not in cleaned
    assert "[1, 2,]" not in cleaned            # trailing comma dropped
    assert "[1, 2]" in cleaned


def test_path_aliases_absent_without_tsconfig(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.code-review-ai]\nrepo_path = "{tmp_path.as_posix()}"\n', encoding="utf-8"
    )
    assert load_config(str(tmp_path)).path_aliases == {}


def test_path_aliases_explicit_config_overrides_tsconfig(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.code-review-ai]\nrepo_path = "{tmp_path.as_posix()}"\n'
        'path_aliases = { "@/" = "app/" }\n',
        encoding="utf-8",
    )
    (tmp_path / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"paths": {"@/*": ["src/*"]}}}),
        encoding="utf-8",
    )
    cfg = load_config(str(tmp_path))
    assert cfg.path_aliases == {"@/": "app/"}  # explicit wins over tsconfig


def test_path_aliases_env_json(monkeypatch, tmp_path):
    monkeypatch.setenv("CRAI_PATH_ALIASES", '{"@/": "src/"}')
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.code-review-ai]\nrepo_path = "{tmp_path.as_posix()}"\n', encoding="utf-8"
    )
    assert load_config(str(tmp_path)).path_aliases == {"@/": "src/"}


def test_dependency_markers_env_comma_split(monkeypatch, tmp_path):
    monkeypatch.setenv("CRAI_DEPENDENCY_MARKERS", "Depends,Inject")
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.code-review-ai]\nrepo_path = "{tmp_path.as_posix()}"\n', encoding="utf-8"
    )
    assert load_config(str(tmp_path)).dependency_markers == ["Depends", "Inject"]


# ── tsconfig baseUrl (JS-M13) ─────────────────────────────────────────


def test_base_url_detected_from_tsconfig(tmp_path):
    """baseUrl from tsconfig compilerOptions flows into config.base_url."""
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.code-review-ai]\nrepo_path = "{tmp_path.as_posix()}"\n', encoding="utf-8"
    )
    (tmp_path / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"baseUrl": "src"}}), encoding="utf-8"
    )
    assert load_config(str(tmp_path)).base_url == "src"


def test_base_url_defaults_empty_without_tsconfig(tmp_path):
    """No tsconfig → base_url stays '' (bare specifiers keep raw resolution)."""
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.code-review-ai]\nrepo_path = "{tmp_path.as_posix()}"\n', encoding="utf-8"
    )
    assert load_config(str(tmp_path)).base_url == ""


def test_base_url_trailing_slash_stripped(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.code-review-ai]\nrepo_path = "{tmp_path.as_posix()}"\n', encoding="utf-8"
    )
    (tmp_path / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"baseUrl": "src/"}}), encoding="utf-8"
    )
    assert load_config(str(tmp_path)).base_url == "src"


def test_base_url_explicit_config_overrides_tsconfig(tmp_path):
    """An explicit base_url in the toml wins over the tsconfig auto-detection."""
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.code-review-ai]\nrepo_path = "{tmp_path.as_posix()}"\n'
        'base_url = "app"\n',
        encoding="utf-8",
    )
    (tmp_path / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"baseUrl": "src"}}), encoding="utf-8"
    )
    assert load_config(str(tmp_path)).base_url == "app"


# ── tsconfig extends chain (JS-M15) ───────────────────────────────────


def test_tsconfig_extends_inherits_base_url(tmp_path):
    """A child tsconfig extends a base — the base's baseUrl flows in."""
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.code-review-ai]\nrepo_path = "{tmp_path.as_posix()}"\n', encoding="utf-8"
    )
    (tmp_path / "tsconfig.base.json").write_text(
        json.dumps({"compilerOptions": {"baseUrl": "src"}}), encoding="utf-8"
    )
    (tmp_path / "tsconfig.json").write_text(
        json.dumps({"extends": "./tsconfig.base.json"}), encoding="utf-8"
    )
    assert load_config(str(tmp_path)).base_url == "src"


def test_tsconfig_extends_merges_paths(tmp_path):
    """Parent and child `paths` both contribute; the child overrides on clash."""
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.code-review-ai]\nrepo_path = "{tmp_path.as_posix()}"\n', encoding="utf-8"
    )
    (tmp_path / "tsconfig.base.json").write_text(
        json.dumps({"compilerOptions": {"paths": {"@base/*": ["lib/*"],
                                                  "shared/*": ["old/*"]}}}),
        encoding="utf-8",
    )
    (tmp_path / "tsconfig.json").write_text(
        json.dumps({"extends": "./tsconfig.base.json",
                    "compilerOptions": {"paths": {"@/*": ["src/*"],
                                                  "shared/*": ["new/*"]}}}),
        encoding="utf-8",
    )
    cfg = load_config(str(tmp_path))
    assert cfg.path_aliases == {"@/": "src/", "@base/": "lib/", "shared/": "new/"}


def test_tsconfig_extends_cycle_terminates(tmp_path):
    """Mutual extends must not loop forever (cycle guard)."""
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.code-review-ai]\nrepo_path = "{tmp_path.as_posix()}"\n', encoding="utf-8"
    )
    (tmp_path / "tsconfig.a.json").write_text(
        json.dumps({"extends": "./tsconfig.b.json",
                    "compilerOptions": {"baseUrl": "src"}}), encoding="utf-8"
    )
    (tmp_path / "tsconfig.b.json").write_text(
        json.dumps({"extends": "./tsconfig.a.json"}), encoding="utf-8"
    )
    (tmp_path / "tsconfig.json").write_text(
        json.dumps({"extends": "./tsconfig.a.json"}), encoding="utf-8"
    )
    assert load_config(str(tmp_path)).base_url == "src"


def test_config_hash_changes_with_base_url():
    """base_url is a hash key — a tsconfig change flips config_hash, which is
    the signal the incremental path uses to fall back to a full rebuild."""
    cfg = load_config(".")
    assert config_hash(cfg) != config_hash(replace(cfg, base_url="src"))
