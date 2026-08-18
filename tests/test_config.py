import json

from code_review_ai.config import Config, _jsonc_clean, load_config


def test_load_config_defaults(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.code-review-ai]\nrepo_path = "."\n', encoding="utf-8"
    )
    cfg = load_config(str(tmp_path))
    assert isinstance(cfg, Config)
    assert cfg.repo_path == "."
    assert cfg.diff_base == "origin/main"        # default
    assert cfg.entry_names == ["main"]            # default heuristic
    assert cfg.dependency_markers == ["Depends"]  # default DI marker
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
